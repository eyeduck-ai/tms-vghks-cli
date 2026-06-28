from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from .batch_login import (
    AccountLoginConfig,
    AccountsLoginConfig,
    DEFAULT_ACCOUNTS_PATH,
    DEFAULT_CAPTCHA_MODE,
    load_accounts_config,
    run_batch_requests_login,
    run_batch_requests_login_from_file,
)
from .captcha_recognizers import (
    PADDLEOCR_SDK_DEFAULT_DIAGNOSTIC_PROFILES,
    compare_paddleocr_sdk_profiles,
    parse_paddleocr_sdk_profiles,
    parse_paddleocr_sdk_tiers,
    recognize_captcha,
)
from .export_pacing import export_pacing_options_from_cli
from .handlers import RunOptions, TmsRunner, serialize_run_result
from .quiz_resolver import GeminiQuizConfig
from .cli_diag import (
    RequestsFormSubmitDiagnosticBatch,
    diagnostic_result_status as _diagnostic_result_status,
    diagnostic_result_success as _diagnostic_result_success,
    requests_form_diagnostic_completed as _requests_form_diagnostic_completed,
    run_requests_form_submit_diagnostic as _run_requests_form_submit_diagnostic,
    select_detail_item as _select_detail_item,
)
from .login_error_probes import (
    DEFAULT_FAKE_PROBE_ACCOUNT,
    DEFAULT_FAKE_PROBE_PASSWORD,
    DEFAULT_WRONG_CAPTCHA,
    LOGIN_ERROR_PROBE_BACKENDS,
    LoginErrorProbeOptions,
    parse_login_error_probe_scenarios,
    run_login_error_probes,
)
from .models import (
    AuthOptions,
    LoginMethod,
    OperationBackend,
    RequestsLoginResult,
    SiteState,
)
from .network_diagnostics import (
    DEFAULT_NETWORK_OBSERVATIONS_PATH,
    NETWORK_DIAGNOSTIC_ACTIONS,
    run_activity_network_diagnostic,
)
from .playwright_form_validation import (
    DEFAULT_VALIDATION_JSONL_PATH,
    DEFAULT_VALIDATION_MARKDOWN_PATH,
    parse_scope,
    validate_playwright_forms,
)
from .playwright_probe import (
    DEFAULT_HISTORICAL_QUIZ_JSONL_PATH,
    DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH,
    DEFAULT_PLAYWRIGHT_JSONL_PATH,
    DEFAULT_PLAYWRIGHT_MARKDOWN_PATH,
    export_question_bank_playwright,
    export_historical_quiz_bank_playwright,
)
from .playwright_kexam import (
    DEFAULT_KEXAM_COURSE,
    DEFAULT_KEXAM_EXAM_URL,
    read_kexam_exam_page_playwright,
    run_playwright_quiz_resubmit_diagnostic,
)
from .privacy import redact_sensitive_value
from .question_bank_export import (
    DEFAULT_EXPORT_PATH,
    export_question_bank,
    parse_include,
)
from .reading_accumulation import (
    DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH,
    run_reading_accumulation_diagnostic,
    select_reading_accumulation_target,
)
from .reference_bank import (
    DEFAULT_REFERENCE_BANK_JSONL_PATH,
    DEFAULT_REFERENCE_BANK_MARKDOWN_PATH,
    build_reference_question_bank,
)
from .requests_login import (
    ajax_login_headers,
    build_login_payload,
    login_response_text_excerpt,
)
from .requests_parity import (
    compare_backend_read_paths,
    format_backend_comparison_markdown,
    build_requests_parity_report,
    format_requests_parity_markdown,
)
from .requests_reproduction import analyze_requests_reproduction_file, format_requests_reproduction_markdown
from .requests_kexam import read_kexam_exam_page_requests, run_requests_kexam_resubmit_diagnostic
from .requests_question_bank import (
    DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH,
    DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH,
    export_historical_quiz_bank_requests,
)
from .requests_watch_time import run_requests_watch_time
from .session import LoginRequired, TmsError, TmsSession, TransientTmsError


PLAYWRIGHT_BACKEND_COMMANDS = {
    "compare-backends",
    "network-diagnostics",
    "reading-accumulation-diagnostics",
    "probe-question-bank-playwright",
    "playwright-kexam-records",
    "playwright-quiz-resubmit-diagnostics",
    "validate-playwright-forms",
}

CLI_PROG = "tms-vghks-cli"


def _maybe_help(text: str | None = None, *, hidden: bool = False) -> str | None:
    return argparse.SUPPRESS if hidden else text


class TmsRootParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tms_group_parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {}
        self._tms_leaf_parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {}

    def register_group_parser(self, path: tuple[str, ...], parser: argparse.ArgumentParser) -> None:
        self._tms_group_parsers[path] = parser

    def register_leaf_parser(self, path: tuple[str, ...], parser: argparse.ArgumentParser) -> None:
        self._tms_leaf_parsers[path] = parser

    def parse_args(self, args=None, namespace=None):
        argv = sys.argv[1:] if args is None else list(args)
        argv, root_cli_mode = self._extract_root_cli_mode(argv)
        if namespace is None:
            namespace = argparse.Namespace()
        if root_cli_mode is not None:
            setattr(namespace, "root_cli_mode", root_cli_mode)
        group_parser = self._group_parser_for_unknown_option(argv)
        if group_parser is not None:
            unknown = self._unknown_args_for_group(argv, group_parser)
            group_parser.error("unrecognized arguments: " + " ".join(map(str, unknown)))
        parsed, unknown = self.parse_known_args(argv, namespace)
        if unknown:
            leaf_parser = self._leaf_parser_for(argv)
            if leaf_parser is not None:
                leaf_parser.error("unrecognized arguments: " + " ".join(map(str, unknown)))
            self.error("unrecognized arguments: " + " ".join(map(str, unknown)))
        self._resolve_cli_mode_argument(parsed)
        return parsed

    def _extract_root_cli_mode(self, argv: list[str]) -> tuple[list[str], str | None]:
        if not argv:
            return argv, None
        root_mode: str | None = None
        cleaned: list[str] = []
        root_scan = True
        for arg in argv:
            if root_scan and arg in {"--human", "--agent"}:
                mode = arg[2:]
                if root_mode is not None and root_mode != mode:
                    self.error("--human and --agent are mutually exclusive")
                root_mode = mode
                continue
            cleaned.append(arg)
            if root_scan and not arg.startswith("-"):
                root_scan = False
        return cleaned, root_mode

    def _resolve_cli_mode_argument(self, parsed: argparse.Namespace) -> None:
        root_mode = getattr(parsed, "root_cli_mode", None)
        leaf_mode = getattr(parsed, "cli_mode", None)
        if root_mode and leaf_mode and root_mode != leaf_mode:
            self.error("--human and --agent are mutually exclusive")
        parsed.cli_mode = leaf_mode or root_mode
        if hasattr(parsed, "root_cli_mode"):
            delattr(parsed, "root_cli_mode")

    def _leaf_parser_for(self, argv: list[str]) -> argparse.ArgumentParser | None:
        best_match: tuple[str, ...] | None = None
        for path in self._tms_leaf_parsers:
            if tuple(argv[: len(path)]) == path and (best_match is None or len(path) > len(best_match)):
                best_match = path
        if best_match is None:
            return None
        return self._tms_leaf_parsers[best_match]

    def _group_parser_for_unknown_option(self, argv: list[str]) -> argparse.ArgumentParser | None:
        best_match: tuple[str, ...] | None = None
        for path in self._tms_group_parsers:
            if tuple(argv[: len(path)]) != path:
                continue
            rest = argv[len(path) :]
            if not rest or rest[0] in {"-h", "--help"} or not rest[0].startswith("-"):
                continue
            if best_match is None or len(path) > len(best_match):
                best_match = path
        if best_match is None:
            return None
        return self._tms_group_parsers[best_match]

    def _unknown_args_for_group(self, argv: list[str], group_parser: argparse.ArgumentParser) -> list[str]:
        for path, parser in self._tms_group_parsers.items():
            if parser is group_parser and tuple(argv[: len(path)]) == path:
                return argv[len(path) :]
        return argv[1:]


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _apply_default_accounts_file(args)
        if args.command == "ocr-sdk-test":
            return handle_ocr_sdk_test(args)
        if args.command == "requests-parity":
            return handle_requests_parity(args)
        if args.command == "analyze-requests-reproduction":
            return handle_analyze_requests_reproduction(args)
        with TmsSession() as session:
            if args.command == "status":
                status = session.is_logged_in(fallback_browser=False)
                print_payload(args, status)
                return 0 if status.state in {SiteState.LOGGED_IN, SiteState.LOGIN_REQUIRED} else 2
            if args.command == "login":
                if args.accounts:
                    return handle_accounts_playwright_login(args)
                status = session.ensure_login(
                    headless=args.headless,
                    timeout_seconds=args.timeout,
                    transient_retries=args.transient_retries,
                    transient_delay_seconds=args.transient_delay_seconds,
                )
                if not args.no_save_session:
                    session.save_session_bundle(args.session_dir)
                print_payload(args, status)
                return 0
            if args.command == "login-diagnostics":
                return handle_login_diagnostics(args)
            if args.command == "login-error-probes":
                return handle_login_error_probes(args)
            if args.command == "network-diagnostics":
                ensure_for_command(session, args)
                return handle_network_diagnostics(session, args)
            if args.command == "reading-accumulation-diagnostics":
                if args.accounts:
                    return handle_accounts_reading_accumulation(args)
                ensure_for_command(session, args)
                return handle_reading_accumulation(session, args)
            if args.command == "requests-reading-accumulation-diagnostics":
                if args.accounts:
                    return handle_accounts_requests_reading_accumulation(args)
                ensure_for_command(session, args)
                return handle_requests_reading_accumulation(session, args)
            if args.command == "requests-form-submit-diagnostics":
                if args.accounts:
                    return handle_accounts_requests_form_submit(args)
                ensure_for_command(session, args)
                return handle_requests_form_submit(session, args)
            if args.command == "requests-kexam-records":
                if args.accounts:
                    return handle_accounts_requests_kexam_records(args)
                ensure_for_command(session, args)
                return handle_requests_kexam_records(session, args)
            if args.command == "requests-quiz-resubmit-diagnostics":
                if args.accounts:
                    return handle_accounts_requests_quiz_resubmit(args)
                ensure_for_command(session, args)
                return handle_requests_quiz_resubmit(session, args)
            if args.command == "playwright-kexam-records":
                if args.accounts:
                    return handle_accounts_playwright_kexam_records(args)
                ensure_for_command(session, args)
                return handle_playwright_kexam_records(session, args)
            if args.command == "playwright-quiz-resubmit-diagnostics":
                if args.accounts:
                    return handle_accounts_playwright_quiz_resubmit(args)
                ensure_for_command(session, args)
                return handle_playwright_quiz_resubmit(session, args)
            if args.command == "login-requests":
                return handle_requests_login(session, args)
            if args.command == "compare-backends":
                if args.accounts:
                    return handle_accounts_compare_backends(args)
                ensure_for_command(session, args)
                return handle_compare_backends(session, args)
            if args.command == "list":
                if args.accounts:
                    return handle_accounts_list(args)
                ensure_for_command(session, args)
                backend = OperationBackend(getattr(args, "backend", OperationBackend.REQUESTS.value))
                courses = (
                    session.list_pending_courses(backend=backend)
                    if args.kind == "pending"
                    else session.list_completed_courses(backend=backend)
                )
                print_payload(args, courses)
                return 0
            if args.command == "inspect-course":
                if args.accounts:
                    return handle_accounts_inspect(args)
                ensure_for_command(session, args)
                backend = OperationBackend(getattr(args, "backend", OperationBackend.REQUESTS.value))
                detail = session.get_course_detail(args.course, backend=backend)
                print_payload(args, detail)
                return 0
            if args.command == "probe-question-bank-playwright":
                pacing_options = _export_pacing_options_from_args(args)
                if args.accounts:
                    return handle_accounts_probe_question_bank_playwright(args)
                ensure_for_command(session, args)
                backend = getattr(args, "backend", "playwright")
                if backend == "requests" and args.historical_quiz_bank:
                    result = export_historical_quiz_bank_requests(
                        session=session,
                        output_path=args.output or DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH,
                        markdown_path=args.markdown
                        if args.markdown is not None
                        else DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH,
                        source_account_label=args.source_account_label,
                        allow_private_export=args.allow_private_export,
                        include_unsubmitted_records=args.include_unsubmitted_records,
                        course_limit=args.course_limit,
                        activity_limit=args.activity_limit,
                        pacing_options=pacing_options,
                    )
                elif backend == "requests":
                    result = export_question_bank(
                        session=session,
                        output_path=args.output or DEFAULT_EXPORT_PATH,
                        include=parse_include(args.include),
                        source_account_label=args.source_account_label,
                        allow_private_export=args.allow_private_export,
                        probe_only=False,
                        pacing_options=pacing_options,
                    )
                elif args.historical_quiz_bank:
                    result = export_historical_quiz_bank_playwright(
                        session=session,
                        output_path=args.output or DEFAULT_HISTORICAL_QUIZ_JSONL_PATH,
                        markdown_path=args.markdown
                        if args.markdown is not None
                        else DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH,
                        source_account_label=args.source_account_label,
                        allow_private_export=args.allow_private_export,
                        include_unsubmitted_records=args.include_unsubmitted_records,
                        course_limit=args.course_limit,
                        activity_limit=args.activity_limit,
                        pacing_options=pacing_options,
                    )
                else:
                    result = export_question_bank_playwright(
                        session=session,
                        output_path=args.output or DEFAULT_PLAYWRIGHT_JSONL_PATH,
                        markdown_path=args.markdown
                        if args.markdown is not None
                        else DEFAULT_PLAYWRIGHT_MARKDOWN_PATH,
                        include=parse_include(args.include),
                        source_account_label=args.source_account_label,
                        allow_private_export=args.allow_private_export,
                        include_unsubmitted_records=args.include_unsubmitted_records,
                        course_limit=args.course_limit,
                        activity_limit=args.activity_limit,
                        pacing_options=pacing_options,
                    )
                print_payload(args, result)
                return 0 if result.record_count else 4
            if args.command == "validate-playwright-forms":
                if args.accounts:
                    return handle_accounts_validate_playwright_forms(args)
                ensure_for_command(session, args)
                result = validate_playwright_forms(
                    session=session,
                    scope=parse_scope(args.scope),
                    include=parse_include(args.include),
                    output_path=args.output,
                    markdown_path=args.markdown,
                    auth_options=auth_options_from_args(args),
                    course_limit=args.course_limit,
                    activity_limit=args.activity_limit,
                    include_unsubmitted_records=args.include_unsubmitted_records,
                )
                print_payload(args, result)
                return 0
            if args.command == "export-question-bank":
                pacing_options = _export_pacing_options_from_args(args)
                if args.accounts:
                    return handle_accounts_export_question_bank(args)
                ensure_for_command(session, args)
                result = export_question_bank(
                    session=session,
                    output_path=args.output,
                    include=parse_include(args.include),
                    source_account_label=args.source_account_label,
                    allow_private_export=args.allow_private_export,
                    probe_only=args.probe_only,
                    pacing_options=pacing_options,
                )
                print_payload(args, result)
                return 0 if result.record_count or args.probe_only else 4
            if args.command == "build-reference-question-bank":
                result = build_reference_question_bank(
                    history_jsonl=args.history,
                    output_jsonl=args.output,
                    output_markdown=args.markdown,
                    ai_suggestions_jsonl=args.ai_suggestions_jsonl,
                    posttest_ai_policy=args.posttest_ai_policy,
                )
                print_payload(args, result)
                return 0
            if args.command == "run":
                if args.accounts:
                    return handle_accounts_run(args)
                auth_options = ensure_for_command(session, args)
                options = run_options_from_args(args, auth_options)
                runner = TmsRunner(session, options)
                result = runner.run_scheduler()
                print_payload(args, serialize_run_result(result))
                if (
                    result.sanitized_question_bank_snippet
                    and args.export_question_bank_snippets
                    and not _output_json(args)
                ):
                    print()
                    print(result.sanitized_question_bank_snippet)
                return 0 if result.success else 3
    except LoginRequired as exc:
        if "args" in locals() and _output_json(args):
            print_json_or_text(True, {"success": False, "error": "login_required", "message": str(exc)})
            return 10
        print(f"login required: {exc}", file=sys.stderr)
        return 10
    except TransientTmsError as exc:
        if "args" in locals() and _output_json(args):
            print_json_or_text(True, {"success": False, "error": "transient_error", "message": str(exc)})
            return 12
        print(f"temporary TMS error: {exc}", file=sys.stderr)
        return 12
    except TmsError as exc:
        if "args" in locals() and _output_json(args):
            print_json_or_text(True, {"success": False, "error": "tms_error", "message": str(exc)})
            return 11
        print(f"TMS error: {exc}", file=sys.stderr)
        return 11
    except ValueError as exc:
        if "args" in locals() and _output_json(args):
            print_json_or_text(True, {"success": False, "error": "invalid_request", "message": str(exc)})
            return 2
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    return 1


def _apply_default_accounts_file(args) -> None:
    if not getattr(args, "use_default_accounts", False) or getattr(args, "accounts", None):
        return
    default_path = Path(DEFAULT_ACCOUNTS_PATH)
    if not default_path.exists():
        if _interactive_allowed(args):
            _stderr_print(f"Warning: default accounts config {default_path} was not found. Falling back to interactive credentials.")
        return
    try:
        load_accounts_config(str(default_path))
    except Exception as exc:
        if _interactive_allowed(args):
            _stderr_print(
                f"Warning: default accounts config {default_path} could not be used: {exc}. "
                "Falling back to interactive credentials."
            )
            return
        raise LoginRequired(f"default accounts config {default_path} could not be used: {exc}") from exc
    args.accounts = str(default_path)


def build_parser() -> argparse.ArgumentParser:
    parser = TmsRootParser(
        prog=CLI_PROG,
        usage=f"{CLI_PROG} [-h] <command> ...",
        description="TMS VGHKS course automation. Use short commands for daily work; grouped commands provide advanced tools.",
        epilog=(
            "Daily commands:\n"
            "  sign-in              Login with the default accounts file or interactive credentials.\n"
            "  pending              List pending courses.\n"
            "  completed            List completed courses.\n"
            "  course <id-or-url>   Inspect one course detail tree.\n"
            "  go                   Complete pending courses with requests first.\n\n"
            "Grouped commands:\n"
            "  auth      Login and session commands.\n"
            "  courses   Course listing and inspection commands.\n"
            "  diag      Network, reading, form, backend, and parity diagnostics.\n"
            "  bank      Question-bank export, probe, and reference-bank build commands.\n\n"
            "Examples:\n"
            f"  uv run {CLI_PROG} sign-in\n"
            f"  uv run {CLI_PROG} pending\n"
            f"  uv run {CLI_PROG} go --quiz auto"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="_entry_command", required=True, metavar="<command>", help=argparse.SUPPRESS)
    _set_subparser_context(sub, parser, ())

    _add_short_command_parsers(sub, parser)
    _add_grouped_command_parsers(sub, parser)
    return parser


def handle_ocr_sdk_test(args) -> int:
    if args.profiles and args.tiers:
        raise ValueError("use either --profiles or --tiers, not both")
    if args.profiles:
        profiles = parse_paddleocr_sdk_profiles(args.profiles)
    elif args.tiers:
        profiles = parse_paddleocr_sdk_tiers(args.tiers)
    else:
        profiles = list(PADDLEOCR_SDK_DEFAULT_DIAGNOSTIC_PROFILES)
    results = compare_paddleocr_sdk_profiles(args.image, profiles)
    payload = {"image": args.image, "results": results}
    if _output_json(args):
        print_json_or_text(True, payload)
    else:
        for result in results:
            confidence = "" if result.confidence is None else f", confidence={result.confidence:.4f}"
            if result.success:
                print(f"{result.profile}: text={result.text!r}{confidence}, elapsed={result.elapsed_seconds:.2f}s")
            else:
                print(f"{result.profile}: {result.status} after {result.elapsed_seconds:.2f}s: {result.error}")
    return 0 if any(result.success for result in results) else 12


def handle_requests_parity(args) -> int:
    reproduction = analyze_requests_reproduction_file(args.observations) if args.observations else None
    report = build_requests_parity_report(reproduction_report=reproduction)
    output_path = ""
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(format_requests_parity_markdown(report), encoding="utf-8", newline="\n")
        output_path = str(output)
    if _output_json(args):
        payload: dict[str, Any] = {"report": report}
        if output_path:
            payload["output_path"] = output_path
        print_json_or_text(True, payload)
    elif output_path:
        print(f"wrote {output_path}")
    else:
        print(format_requests_parity_markdown(report), end="")
    return 0


def handle_compare_backends(session: TmsSession, args) -> int:
    report = compare_backend_read_paths(
        session,
        course=args.course,
        detail_limit=args.detail_limit,
        include_pending=not args.skip_pending,
        include_completed=not args.skip_completed,
    )
    if _output_json(args):
        print_json_or_text(True, {"report": report})
    else:
        print(format_backend_comparison_markdown(report), end="")
    if report.status == "equivalent":
        return 0
    if report.status == "error":
        return 12
    return 4


def handle_analyze_requests_reproduction(args) -> int:
    report = analyze_requests_reproduction_file(args.input)
    output_path = ""
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(format_requests_reproduction_markdown(report), encoding="utf-8", newline="\n")
        output_path = str(output)
    if _output_json(args):
        payload: dict[str, Any] = {"report": report}
        if output_path:
            payload["output_path"] = output_path
        print_json_or_text(True, payload)
    elif output_path:
        print(f"wrote {output_path}")
    else:
        print(format_requests_reproduction_markdown(report), end="")
    return 0


def handle_accounts_playwright_login(args) -> int:
    config = _accounts_config_from_args(args)
    results: list[dict[str, Any]] = []
    for account in config.accounts:
        try:
            with TmsSession(base_url=config.base_url) as account_session:
                result = account_session.login_playwright_with_ocr(
                    account=account.account,
                    password=account.password,
                    session_dir=account.session_dir,
                    captcha_mode=config.captcha_mode,
                    headless=args.headless,
                    timeout_seconds=args.timeout,
                    transient_retries=args.transient_retries,
                    transient_delay_seconds=args.transient_delay_seconds,
                    save=not args.no_save_session,
                    ocr_config=config.ocr,
                )
                result = {"label": account.label, **result}
                if result.get("success") and args.verify_courses:
                    pending = account_session.list_pending_courses_playwright()
                    completed = account_session.list_completed_courses_playwright()
                    result["pending_count"] = len(pending)
                    result["completed_count"] = len(completed)
        except Exception as exc:
            result = _account_operation_exception(account, exc)
        results.append(result)
    return _print_account_operation_payload(args, results)


def handle_login_diagnostics(args) -> int:
    config = _accounts_config_from_args(args)
    results = [_run_login_diagnostic(config, account, args, config.captcha_mode) for account in config.accounts]
    return _print_account_operation_payload(args, results)


def handle_login_error_probes(args) -> int:
    config = _accounts_config_from_args(args)
    options = LoginErrorProbeOptions(
        backend=args.backend,
        scenarios=args.scenarios,
        captcha_mode=config.captcha_mode,
        wrong_captcha=args.wrong_captcha,
        fake_account=args.fake_account,
        fake_password=args.fake_password,
        headless=args.headless,
        transient_retries=args.transient_retries,
        transient_delay_seconds=args.transient_delay_seconds,
    )
    results = run_login_error_probes(config, options)
    return _print_account_operation_payload(args, results)


def handle_network_diagnostics(session: TmsSession, args) -> int:
    detail = session.get_course_detail(args.course)
    item = _select_detail_item(detail, args.item_title, args.item_order)
    result = run_activity_network_diagnostic(
        session=session,
        course=detail,
        item=item,
        output_path=args.output,
        headless=args.headless,
        wait_ms=args.wait_ms,
        action=args.action,
    )
    print_payload(args, result)
    return 0


def handle_reading_accumulation(session: TmsSession, args) -> int:
    result = run_reading_accumulation_diagnostic(
        session=session,
        course=args.course,
        item_title=args.item_title,
        item_order=args.item_order,
        output_path=args.output or DEFAULT_READING_ACCUMULATION_OBSERVATIONS_PATH,
        headless=args.headless,
        wait_seconds=args.wait_seconds,
        poll_seconds=args.poll_seconds,
        course_limit=args.course_limit,
    )
    print_payload(args, result)
    return 0 if result.status not in {"blocked"} else 3


def handle_requests_reading_accumulation(session: TmsSession, args) -> int:
    target = select_reading_accumulation_target(
        session,
        course=args.course,
        item_title=args.item_title,
        item_order=args.item_order,
        course_limit=args.course_limit,
    )
    result = run_requests_watch_time(
        session=session,
        course=target.course,
        item=target.item,
        wait_seconds=args.wait_seconds,
        force_watch_time=args.force_watch_time,
    )
    print_payload(args, result)
    return 0 if _requests_reading_diagnostic_completed(result.status) else 3


def handle_requests_form_submit(session: TmsSession, args) -> int:
    result = _run_requests_form_submit_diagnostic(session, args)
    print_payload(args, result)
    return 0 if _requests_form_diagnostic_completed(result, probe_only=args.probe_only) else 3


def handle_requests_kexam_records(session: TmsSession, args) -> int:
    result = read_kexam_exam_page_requests(
        session=session,
        exam_url=args.exam_url,
        include_unsubmitted_records=args.include_unsubmitted_records,
    )
    print_payload(args, result)
    return 0 if result.success else 3


def handle_requests_quiz_resubmit(session: TmsSession, args) -> int:
    result = run_requests_kexam_resubmit_diagnostic(
        session=session,
        course=args.course,
        exam_url=args.exam_url,
        quiz_policy=args.quiz,
        question_bank_path=args.question_bank,
        gemini_config=getattr(args, "gemini_config", None),
        probe_only=args.probe_only,
    )
    print_payload(args, result)
    return 0 if result.success else 3


def handle_playwright_kexam_records(session: TmsSession, args) -> int:
    result = read_kexam_exam_page_playwright(
        session=session,
        exam_url=args.exam_url,
        include_unsubmitted_records=args.include_unsubmitted_records,
        headless=args.headless,
    )
    print_payload(args, result)
    return 0 if result.success else 3


def handle_playwright_quiz_resubmit(session: TmsSession, args) -> int:
    result = run_playwright_quiz_resubmit_diagnostic(
        session=session,
        course=args.course,
        exam_url=args.exam_url,
        quiz_policy=args.quiz,
        question_bank_path=args.question_bank,
        auth_options=auth_options_from_args(args),
        headless=args.headless,
    )
    print_payload(args, result)
    return 0 if result.success else 3


def _run_login_diagnostic(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    args,
    captcha_mode: str,
) -> dict[str, Any]:
    session_dir = Path(account.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    playwright_events: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "label": account.label,
        "session_dir": account.session_dir,
        "success": False,
        "status": "diagnostic_failed",
    }
    try:
        with TmsSession(base_url=config.base_url) as playwright_session:
            playwright_session.start_browser(headless=args.headless)
            assert playwright_session.page is not None
            _attach_login_network_capture(playwright_session.page, playwright_events)
            playwright_result = playwright_session.login_playwright_with_ocr(
                account=account.account,
                password=account.password,
                session_dir=account.session_dir,
                captcha_mode=captcha_mode,
                headless=args.headless,
                timeout_seconds=120,
                transient_retries=args.transient_retries,
                transient_delay_seconds=args.transient_delay_seconds,
                save=True,
                ocr_config=config.ocr,
            )
    except Exception as exc:
        playwright_result = {"success": False, "status": "error", "message": str(exc)}

    requests_attempt = _run_requests_login_diagnostic(config, account, args, captcha_mode)
    payload.update(
        {
            "success": bool(playwright_result.get("success")),
            "status": "ok" if playwright_result.get("success") else "playwright_login_failed",
            "playwright_login": playwright_result,
            "playwright_login_posts": playwright_events,
            "requests_login": requests_attempt,
        }
    )
    diagnostics_path = session_dir / "login_diagnostics.json"
    diagnostics_path.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    payload["diagnostics_path"] = str(diagnostics_path)
    return payload


def _run_requests_login_diagnostic(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    args,
    captcha_mode: str,
) -> dict[str, Any]:
    captcha_path = Path(account.session_dir) / "requests_diagnostic_captcha.jpg"
    try:
        with TmsSession(base_url=config.base_url) as requests_session:
            challenge = requests_session.prepare_requests_login(
                captcha_path=captcha_path,
                show_captcha=False,
                session_dir=account.session_dir,
            )
            ocr_result = recognize_captcha(challenge.captcha_path or captcha_path, config.ocr, captcha_mode)
            payload_keys = sorted(build_login_payload(challenge, account=account.account, password=account.password, captcha=ocr_result.text))
            result = requests_session.submit_requests_login(
                account=account.account,
                password=account.password,
                captcha=ocr_result.text,
                challenge=challenge,
                save=False,
                session_dir=account.session_dir,
                transient_retries=args.transient_retries,
                transient_delay_seconds=args.transient_delay_seconds,
            )
            return {
                "success": result.success,
                "status": result.status,
                "message": result.message,
                "action_url": challenge.action_url,
                "response_status_code": result.response_status_code,
                "redirect_url": result.redirect_url,
                "login_state_after_post": result.login_state_after_post,
                "set_cookie_names": result.set_cookie_names,
                "request_header_keys": sorted(ajax_login_headers(challenge.login_url)),
                "payload_keys": payload_keys,
                "response_json_summary": _login_json_summary(result.response_json),
                "ocr_source": ocr_result.source,
                "ocr_confidence": ocr_result.confidence,
                "handled_multi_login": result.handled_multi_login,
                "multi_login_action": result.multi_login_action,
                "multi_login_status": result.multi_login_status,
                "multi_login_response_status_code": result.multi_login_response_status_code,
            }
    except Exception as exc:
        return {"success": False, "status": "error", "message": str(exc)}


def _attach_login_network_capture(page, events: list[dict[str, Any]]) -> None:
    def on_response(response) -> None:
        try:
            request = response.request
            if request.method.upper() != "POST":
                return
            post_data = _request_post_data(request)
            event = {
                "method": request.method,
                "url": request.url,
                "status": response.status,
                "redirect_url": response.headers.get("location"),
                "content_type": response.headers.get("content-type"),
                "request_header_keys": sorted(request.headers),
                "response_header_keys": sorted(response.headers),
                "payload_keys": _post_data_keys(post_data),
            }
            if "/cdn-cgi/rum" in request.url:
                event["kind"] = "telemetry"
                event["payload_keys"] = []
            if "/index/login" in request.url:
                event["kind"] = "login_post"
                event["response_json_summary"] = _login_post_response_summary(response)
            events.append(event)
        except Exception as exc:
            events.append({"capture_error": str(exc)})

    page.on("response", on_response)


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
            if isinstance(data, dict):
                return sorted(str(key) for key in data)[:50]
            return [f"<json:{type(data).__name__}>"]
        except Exception:
            return ["<json>"]
    try:
        return sorted({key for key, _ in parse_qsl(text, keep_blank_values=True) if len(key) <= 80})[:80]
    except Exception:
        return []


def _login_post_response_summary(response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {"json": False}
    return _login_json_summary(data)


def _login_json_summary(data: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"json": True}
    if isinstance(data, dict):
        summary["keys"] = sorted(str(key) for key in data)[:50]
        ret = data.get("ret")
        if isinstance(ret, dict):
            summary["ret"] = {
                str(key): value
                for key in ("status", "msg", "message", "action")
                if isinstance((value := ret.get(key)), (str, int, float, bool, type(None)))
            }
            if "action" in ret and "action" not in summary["ret"]:
                summary["ret"]["action_summary"] = _summarize_login_action(ret["action"])
        elif isinstance(ret, (str, int, float, bool, type(None))):
            summary["ret"] = ret
    else:
        summary["type"] = type(data).__name__
    return summary


def _summarize_login_action(value: Any) -> Any:
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [_summarize_login_action(item) for item in value[:5]],
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
                selected[key_text] = _redact_diagnostic_string(item[:300]) if isinstance(item, str) else redact_sensitive_value(item)
        return {
            "type": "dict",
            "keys": sorted(str(key) for key in value)[:50],
            "selected": selected,
        }
    if isinstance(value, str):
        return {"type": "str", "excerpt": _redact_diagnostic_string(value[:300])}
    return {"type": type(value).__name__}


def _redact_diagnostic_string(value: str) -> str:
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


def handle_requests_login(session: TmsSession, args) -> int:
    session.configure_transient_policy(
        getattr(args, "transient_retries", None),
        getattr(args, "transient_delay_seconds", None),
    )
    if args.login_requests_command == "prepare":
        if args.wait and not _interactive_allowed(args):
            raise ValueError("--wait requires --human with TTY stdin")
        challenge = session.prepare_requests_login(
            captcha_path=args.captcha_path,
            show_captcha=args.show_captcha,
            session_dir=args.session_dir,
        )
        print_payload(args, challenge)
        if args.show_captcha:
            _stderr_print(f"captcha image: {challenge.captcha_path}")
            if _interactive_allowed(args) and (args.wait or not args.no_wait):
                _stderr_input("Captcha image is open in Playwright. Press Enter to continue...")
        return 0
    if args.login_requests_command == "submit":
        result = session.submit_requests_login(
            account=args.account,
            password=args.password,
            captcha=args.captcha,
            save=not args.no_save,
            session_dir=args.session_dir,
            allow_blank=args.allow_blank,
            transient_retries=args.transient_retries,
            transient_delay_seconds=args.transient_delay_seconds,
        )
        print_payload(args, result)
        return 0 if result.success else 12
    if args.login_requests_command == "load-state":
        paths = session.load_session_bundle(args.session_dir)
        status = session.is_logged_in()
        print_payload(args, {"paths": paths, "status": status})
        return 0 if status.logged_in else 13
    if args.login_requests_command == "login":
        return handle_requests_login_accounts(args)
    if args.login_requests_command == "auto":
        return handle_requests_login_auto(session, args)
    if args.login_requests_command == "probe-wrong-captcha":
        return handle_requests_wrong_captcha_probe(session, args)
    if args.login_requests_command == "batch":
        input_func = _input_func_for_args(args)
        print_func = _print_func_for_args(args)
        result = run_batch_requests_login_from_file(
            path=args.accounts,
            captcha_mode=DEFAULT_CAPTCHA_MODE,
            concurrency=args.concurrency,
            show_captcha=args.show_captcha,
            transient_retries=args.transient_retries,
            transient_delay_seconds=args.transient_delay_seconds,
            input_func=input_func,
            print_func=print_func,
        )
        print_payload(args, result)
        return 0 if result.success else 12
    return 1


def handle_requests_login_accounts(args) -> int:
    input_func = _input_func_for_args(args)
    print_func = _print_func_for_args(args)
    config = _requests_login_config_from_args(args)
    result = run_batch_requests_login(
        config,
        captcha_mode=DEFAULT_CAPTCHA_MODE,
        concurrency=args.concurrency,
        show_captcha=args.show_captcha,
        transient_retries=args.transient_retries,
        transient_delay_seconds=args.transient_delay_seconds,
        input_func=input_func,
        print_func=print_func,
    )
    print_payload(args, result)
    return 0 if result.success else 12


def handle_requests_login_auto(session: TmsSession, args) -> int:
    input_func = _input_func_for_args(args)
    print_func = _print_func_for_args(args)
    config = _auto_login_config_from_args(args)
    result = run_batch_requests_login(
        config,
        captcha_mode=DEFAULT_CAPTCHA_MODE,
        concurrency=1,
        show_captcha=args.show_captcha,
        transient_retries=args.transient_retries,
        transient_delay_seconds=args.transient_delay_seconds,
        input_func=input_func,
        print_func=print_func,
    )
    print_payload(args, result)
    return 0 if result.success else 12


def _auto_login_config_from_args(args) -> AccountsLoginConfig:
    if args.accounts:
        config = load_accounts_config(args.accounts)
        account = _select_config_account(config.accounts, args.label, args.accounts)
        return AccountsLoginConfig(
            base_url=config.base_url,
            session_root=config.session_root,
            captcha_mode=config.captcha_mode,
            concurrency=1,
            ocr=config.ocr,
            accounts=[account],
        )

    if not args.account and not args.password:
        account, password = _prompt_credentials_for_args(args, reason="No --accounts file or credentials were provided.")
        return AccountsLoginConfig(
            concurrency=1,
            accounts=[
                AccountLoginConfig(
                    label=args.label or "account",
                    account=account,
                    password=password,
                    session_dir=args.session_dir,
                )
            ],
        )
    if not args.account or not args.password:
        raise ValueError("auth requests-auto requires --accounts or both --account and --password")
    label = args.label or "account"
    return AccountsLoginConfig(
        concurrency=1,
        accounts=[AccountLoginConfig(label=label, account=args.account, password=args.password, session_dir=args.session_dir)],
    )


def _requests_login_config_from_args(args) -> AccountsLoginConfig:
    if args.accounts:
        return _accounts_config_from_args(args)
    if getattr(args, "account", "") or getattr(args, "password", ""):
        if not args.account or not args.password:
            raise ValueError("auth requests-login requires both --account and --password when --accounts is omitted")
        return AccountsLoginConfig(
            concurrency=1,
            accounts=[
                AccountLoginConfig(
                    label=getattr(args, "label", None) or "account",
                    account=args.account,
                    password=args.password,
                    session_dir=getattr(args, "session_dir", ".tms_session"),
                )
            ],
        )
    default_path = Path(DEFAULT_ACCOUNTS_PATH)
    if default_path.exists():
        try:
            return _accounts_config_from_args(argparse.Namespace(**{**vars(args), "accounts": str(default_path)}))
        except Exception as exc:
            if not _interactive_allowed(args):
                raise LoginRequired(f"default accounts config {default_path} could not be used: {exc}") from exc
            _stderr_print(
                f"Warning: default accounts config {default_path} could not be used: {exc}. "
                "Falling back to interactive credentials."
            )
    elif _interactive_allowed(args):
        _stderr_print(f"Warning: default accounts config {default_path} was not found. Falling back to interactive credentials.")
    if not _interactive_allowed(args):
        raise LoginRequired(f"{DEFAULT_ACCOUNTS_PATH} was not found; pass --accounts or --account/--password")
    account, password = _prompt_credentials_for_args(args, reason="No usable accounts config was available.")
    return AccountsLoginConfig(
        concurrency=1,
        accounts=[
            AccountLoginConfig(
                label=getattr(args, "label", None) or "account",
                account=account,
                password=password,
                session_dir=getattr(args, "session_dir", ".tms_session"),
            )
        ],
    )


def _accounts_config_from_args(args) -> AccountsLoginConfig:
    config = load_accounts_config(args.accounts)
    accounts = _filter_config_accounts(config.accounts, getattr(args, "label", None), args.accounts)
    return AccountsLoginConfig(
        base_url=config.base_url,
        session_root=config.session_root,
        captcha_mode=config.captcha_mode,
        concurrency=config.concurrency,
        ocr=config.ocr,
        gemini=config.gemini,
        accounts=accounts,
    )


def _filter_config_accounts(accounts: list[AccountLoginConfig], label: str | None, source_path: str) -> list[AccountLoginConfig]:
    if not label:
        return list(accounts)
    for account in accounts:
        if account.label == label:
            return [account]
    raise ValueError(f"account label {label!r} was not found in {source_path}")


def _select_config_account(accounts: list[AccountLoginConfig], label: str | None, source_path: str) -> AccountLoginConfig:
    if label:
        for account in accounts:
            if account.label == label:
                return account
        raise ValueError(f"account label {label!r} was not found in {source_path}")
    if len(accounts) == 1:
        return accounts[0]
    labels = ", ".join(account.label for account in accounts)
    raise ValueError(f"{source_path} has multiple accounts; pass --label with one of: {labels}")


def handle_requests_wrong_captcha_probe(session: TmsSession, args) -> int:
    challenge = session.prepare_requests_login(
        captcha_path=Path(args.session_dir) / "captcha.jpg",
        show_captcha=False,
        session_dir=args.session_dir,
    )
    result = session.submit_requests_login(
        account=args.account,
        password=args.password,
        captcha=args.captcha,
        challenge=challenge,
        save=False,
        session_dir=args.session_dir,
        transient_retries=args.transient_retries,
        transient_delay_seconds=args.transient_delay_seconds,
    )
    payload = {
        "probe_response_path": _write_wrong_captcha_probe(args.session_dir, result),
        "result": result,
    }
    print_payload(args, payload)
    return 0 if result.status in {"captcha_failed", "credential_failed", "login_failed"} else 12


def handle_accounts_list(args) -> int:
    config = _accounts_config_from_args(args)
    backend = OperationBackend(args.backend)
    results = [
        _run_account_operation(
            config,
            account,
            args,
            lambda account_session: _list_courses_for_backend(account_session, args.kind, backend),
        )
        for account in config.accounts
    ]
    return _print_account_operation_payload(args, results)


def handle_accounts_inspect(args) -> int:
    config = _accounts_config_from_args(args)
    backend = OperationBackend(args.backend)
    results = [
        _run_account_operation(
            config,
            account,
            args,
            lambda account_session: _get_course_detail_for_backend(account_session, args.course, backend),
        )
        for account in config.accounts
    ]
    return _print_account_operation_payload(args, results)


def handle_accounts_compare_backends(args) -> int:
    config = _accounts_config_from_args(args)
    results = []
    for account in config.accounts:
        result = _run_account_operation(
            config,
            account,
            args,
            lambda account_session: compare_backend_read_paths(
                account_session,
                course=args.course,
                detail_limit=args.detail_limit,
                include_pending=not args.skip_pending,
                include_completed=not args.skip_completed,
            ),
        )
        report = result.get("result")
        if result.get("success") and getattr(report, "status", "error") != "equivalent":
            result["success"] = False
            result["status"] = getattr(report, "status", "mismatch")
        results.append(result)
    return _print_compare_account_payload(args, results)


def _list_courses_for_backend(account_session, kind: str, backend: OperationBackend):
    if backend == OperationBackend.PLAYWRIGHT:
        return (
            account_session.list_pending_courses_playwright()
            if kind == "pending"
            else account_session.list_completed_courses_playwright()
        )
    if backend == OperationBackend.HYBRID:
        pending_hybrid = getattr(account_session, "list_pending_courses_hybrid", None)
        completed_hybrid = getattr(account_session, "list_completed_courses_hybrid", None)
        if kind == "pending" and callable(pending_hybrid):
            return pending_hybrid()
        if kind == "completed" and callable(completed_hybrid):
            return completed_hybrid()
    return account_session.list_pending_courses() if kind == "pending" else account_session.list_completed_courses()


def _get_course_detail_for_backend(account_session, course: str, backend: OperationBackend):
    if backend == OperationBackend.PLAYWRIGHT:
        return account_session.get_course_detail_playwright(course)
    if backend == OperationBackend.HYBRID:
        hybrid_detail = getattr(account_session, "get_course_detail_hybrid", None)
        if callable(hybrid_detail):
            return hybrid_detail(course)
    return account_session.get_course_detail(course)


def handle_accounts_reading_accumulation(args) -> int:
    config = _accounts_config_from_args(args)
    results = []
    for account in config.accounts:
        try:
            with TmsSession(base_url=config.base_url) as account_session:
                _ensure_account_session_authenticated(config, account, args, account_session)
                output = args.output or str(Path(account.session_dir) / "reading_accumulation_observations.jsonl")
                result = run_reading_accumulation_diagnostic(
                    session=account_session,
                    course=args.course,
                    item_title=args.item_title,
                    item_order=args.item_order,
                    output_path=output,
                    headless=args.headless,
                    wait_seconds=args.wait_seconds,
                    poll_seconds=args.poll_seconds,
                    course_limit=args.course_limit,
                )
            results.append(
                _account_operation_result(
                    account,
                    success=result.status not in {"blocked"},
                    status=result.status,
                    result=result,
                )
            )
        except Exception as exc:
            results.append(_account_operation_exception(account, exc))
    return _print_account_operation_payload(args, results)


def handle_accounts_requests_reading_accumulation(args) -> int:
    config = _accounts_config_from_args(args)
    results = []
    for account in config.accounts:
        try:
            with TmsSession(base_url=config.base_url) as account_session:
                _ensure_account_session_authenticated(config, account, args, account_session)
                target = select_reading_accumulation_target(
                    account_session,
                    course=args.course,
                    item_title=args.item_title,
                    item_order=args.item_order,
                    course_limit=args.course_limit,
                )
                result = run_requests_watch_time(
                    session=account_session,
                    course=target.course,
                    item=target.item,
                    wait_seconds=args.wait_seconds,
                    force_watch_time=args.force_watch_time,
                )
            results.append(
                _account_operation_result(
                    account,
                    success=_requests_reading_diagnostic_completed(result.status),
                    status=result.status,
                    result=result,
                )
            )
        except Exception as exc:
            results.append(_account_operation_exception(account, exc))
    return _print_account_operation_payload(args, results)


def handle_accounts_requests_form_submit(args) -> int:
    def operation(account_session, account, config, auth_options):
        args.gemini_config = config.gemini
        result = _run_requests_form_submit_diagnostic(account_session, args)
        return result.success, result.status, result

    return _run_accounts_with_config(args, operation)


def handle_accounts_requests_kexam_records(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = read_kexam_exam_page_requests(
            session=account_session,
            exam_url=args.exam_url,
            include_unsubmitted_records=args.include_unsubmitted_records,
        )
        return result.success, result.status, result

    return _run_accounts_with_config(args, operation)


def handle_accounts_requests_quiz_resubmit(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = run_requests_kexam_resubmit_diagnostic(
            session=account_session,
            course=args.course,
            exam_url=args.exam_url,
            quiz_policy=args.quiz,
            question_bank_path=args.question_bank,
            gemini_config=config.gemini,
            probe_only=args.probe_only,
        )
        return result.success, result.status, result

    return _run_accounts_with_config(args, operation)


def handle_accounts_playwright_kexam_records(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = read_kexam_exam_page_playwright(
            session=account_session,
            exam_url=args.exam_url,
            include_unsubmitted_records=args.include_unsubmitted_records,
            headless=args.headless,
        )
        return result.success, result.status, result

    return _run_accounts_with_config(args, operation)


def handle_accounts_playwright_quiz_resubmit(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = run_playwright_quiz_resubmit_diagnostic(
            session=account_session,
            course=args.course,
            exam_url=args.exam_url,
            quiz_policy=args.quiz,
            question_bank_path=args.question_bank,
            auth_options=auth_options,
            headless=args.headless,
            gemini_config=config.gemini,
        )
        return result.success, result.status, result

    return _run_accounts_with_config(args, operation)


def handle_accounts_export_question_bank(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = export_question_bank(
            session=account_session,
            output_path=_account_scoped_default_path(args.output, DEFAULT_EXPORT_PATH, account.label),
            include=parse_include(args.include),
            source_account_label=args.source_account_label or account.label,
            allow_private_export=args.allow_private_export,
            probe_only=args.probe_only,
            pacing_options=_export_pacing_options_from_args(args),
        )
        success = bool(result.record_count or args.probe_only)
        return success, "question_bank_export_completed" if success else "question_bank_export_empty", result

    return _run_accounts_with_config(args, operation)


def handle_accounts_probe_question_bank_playwright(args) -> int:
    def operation(account_session, account, config, auth_options):
        backend = getattr(args, "backend", "playwright")
        pacing_options = _export_pacing_options_from_args(args)
        if backend == "requests" and args.historical_quiz_bank:
            default_output = DEFAULT_REQUESTS_HISTORICAL_QUIZ_JSONL_PATH
            default_markdown = DEFAULT_REQUESTS_HISTORICAL_QUIZ_MARKDOWN_PATH
            result = export_historical_quiz_bank_requests(
                session=account_session,
                output_path=args.output or _account_scoped_default_path(default_output, default_output, account.label),
                markdown_path=args.markdown
                if args.markdown is not None
                else _account_scoped_default_path(default_markdown, default_markdown, account.label),
                source_account_label=args.source_account_label or account.label,
                allow_private_export=args.allow_private_export,
                include_unsubmitted_records=args.include_unsubmitted_records,
                course_limit=args.course_limit,
                activity_limit=args.activity_limit,
                pacing_options=pacing_options,
            )
        elif backend == "requests":
            default_output = DEFAULT_EXPORT_PATH
            result = export_question_bank(
                session=account_session,
                output_path=args.output or _account_scoped_default_path(default_output, default_output, account.label),
                include=parse_include(args.include),
                source_account_label=args.source_account_label or account.label,
                allow_private_export=args.allow_private_export,
                probe_only=False,
                pacing_options=pacing_options,
            )
        elif args.historical_quiz_bank:
            default_output = DEFAULT_HISTORICAL_QUIZ_JSONL_PATH
            default_markdown = DEFAULT_HISTORICAL_QUIZ_MARKDOWN_PATH
            result = export_historical_quiz_bank_playwright(
                session=account_session,
                output_path=args.output or _account_scoped_default_path(default_output, default_output, account.label),
                markdown_path=args.markdown
                if args.markdown is not None
                else _account_scoped_default_path(default_markdown, default_markdown, account.label),
                source_account_label=args.source_account_label or account.label,
                allow_private_export=args.allow_private_export,
                include_unsubmitted_records=args.include_unsubmitted_records,
                course_limit=args.course_limit,
                activity_limit=args.activity_limit,
                pacing_options=pacing_options,
            )
        else:
            default_output = DEFAULT_PLAYWRIGHT_JSONL_PATH
            default_markdown = DEFAULT_PLAYWRIGHT_MARKDOWN_PATH
            result = export_question_bank_playwright(
                session=account_session,
                output_path=args.output or _account_scoped_default_path(default_output, default_output, account.label),
                markdown_path=args.markdown
                if args.markdown is not None
                else _account_scoped_default_path(default_markdown, default_markdown, account.label),
                include=parse_include(args.include),
                source_account_label=args.source_account_label or account.label,
                allow_private_export=args.allow_private_export,
                include_unsubmitted_records=args.include_unsubmitted_records,
                course_limit=args.course_limit,
                activity_limit=args.activity_limit,
                pacing_options=pacing_options,
            )
        return bool(result.record_count), "question_bank_probe_completed" if result.record_count else "question_bank_probe_empty", result

    return _run_accounts_with_config(args, operation)


def _export_pacing_options_from_args(args):
    return export_pacing_options_from_cli(
        delay_min_ms=getattr(args, "delay_min_ms", None),
        delay_max_ms=getattr(args, "delay_max_ms", None),
        no_random_delay=bool(getattr(args, "no_random_delay", False)),
        delay_seed=getattr(args, "delay_seed", None),
    )


def handle_accounts_validate_playwright_forms(args) -> int:
    def operation(account_session, account, config, auth_options):
        result = validate_playwright_forms(
            session=account_session,
            scope=parse_scope(args.scope),
            include=parse_include(args.include),
            output_path=_account_scoped_default_path(args.output, DEFAULT_VALIDATION_JSONL_PATH, account.label),
            markdown_path=_account_scoped_default_path(args.markdown, DEFAULT_VALIDATION_MARKDOWN_PATH, account.label)
            if args.markdown
            else None,
            auth_options=auth_options,
            course_limit=args.course_limit,
            activity_limit=args.activity_limit,
            include_unsubmitted_records=args.include_unsubmitted_records,
        )
        return True, "playwright_form_validation_completed", result

    return _run_accounts_with_config(args, operation)


def handle_accounts_run(args) -> int:
    def operation(account_session, account, config, auth_options):
        options = run_options_from_args(args, auth_options, gemini_config=config.gemini)
        runner = TmsRunner(account_session, options)
        result = runner.run_scheduler()
        return result.success, str(result.state), serialize_run_result(result)

    return _run_accounts_with_config(args, operation)


def _run_accounts_with_config(args, operation) -> int:
    config = _accounts_config_from_args(args)
    results = []
    for account in config.accounts:
        try:
            with TmsSession(base_url=config.base_url) as account_session:
                auth_options = _ensure_account_session_authenticated(config, account, args, account_session)
                success, status, result = operation(account_session, account, config, auth_options)
            results.append(
                _account_operation_result(
                    account,
                    success=success,
                    status=status,
                    result=result,
                )
            )
        except Exception as exc:
            results.append(_account_operation_exception(account, exc))
    return _print_account_operation_payload(args, results)


def run_options_from_args(args, auth_options: AuthOptions, gemini_config=None) -> RunOptions:
    return RunOptions(
        concurrency=args.concurrency,
        max_concurrency=args.max_concurrency,
        adaptive=not args.no_adaptive,
        survey_policy=args.survey,
        quiz_policy=args.quiz,
        question_bank_path=args.question_bank,
        export_question_bank_snippets=args.export_question_bank_snippets,
        dry_run=args.dry_run,
        interactive=_interactive_allowed(args),
        headless=args.headless,
        max_wait_seconds=args.max_wait_seconds,
        backend=OperationBackend(args.backend),
        auth_options=auth_options,
        gemini_config=gemini_config or GeminiQuizConfig(),
        transient_retries=args.transient_retries,
        transient_delay_seconds=args.transient_delay_seconds,
    )


def _run_account_operation(config: AccountsLoginConfig, account: AccountLoginConfig, args, operation) -> dict[str, Any]:
    try:
        with TmsSession(base_url=config.base_url) as account_session:
            _ensure_account_session_authenticated(config, account, args, account_session)
            result = operation(account_session)
        return _account_operation_result(account, success=True, status="ok", result=result)
    except Exception as exc:
        return _account_operation_exception(account, exc)


def _account_auth_options(args, account: AccountLoginConfig) -> AuthOptions:
    method = LoginMethod(getattr(args, "login_method", LoginMethod.AUTO.value))
    return AuthOptions(
        login_method=method,
        session_dir=account.session_dir,
        account=account.account,
        password=account.password,
        captcha=getattr(args, "captcha", ""),
        save_session=not getattr(args, "no_save_session", False),
        headless=getattr(args, "headless", False),
        transient_retries=getattr(args, "transient_retries", 3),
        transient_delay_seconds=getattr(args, "transient_delay_seconds", 2.0),
    )


def _account_saved_auth_options(args, account: AccountLoginConfig) -> AuthOptions:
    auth_options = _account_auth_options(args, account)
    auth_options.login_method = LoginMethod.SAVED
    return auth_options


def _ensure_account_session_authenticated(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    args,
    account_session: TmsSession,
) -> AuthOptions:
    account_session.configure_transient_policy(args.transient_retries, args.transient_delay_seconds)
    if hasattr(account_session, "browser_headless"):
        account_session.browser_headless = bool(getattr(args, "headless", False))
    selected_backend = _backend_for_command(args)
    if hasattr(account_session, "use_backend"):
        account_session.use_backend(selected_backend)
    auth_options = _account_auth_options(args, account)
    method = LoginMethod(auth_options.login_method)
    saved_auth = _account_saved_auth_options(args, account)

    if method == LoginMethod.SAVED:
        _ensure_saved_account_session(account_session, saved_auth, args)
        return saved_auth

    if method == LoginMethod.PLAYWRIGHT:
        _login_account_with_playwright(config, account, args, account_session)
        return saved_auth

    if method == LoginMethod.REQUESTS:
        _login_account_with_requests(config, account, args)
        _ensure_saved_account_session(account_session, saved_auth, args)
        return saved_auth

    try:
        _ensure_saved_account_session(account_session, saved_auth, args)
        return saved_auth
    except TransientTmsError:
        raise
    except (LoginRequired, TmsError):
        if selected_backend == OperationBackend.PLAYWRIGHT:
            _login_account_with_playwright(config, account, args, account_session)
        else:
            _login_account_with_requests(config, account, args)
        _ensure_saved_account_session(account_session, saved_auth, args)
        return saved_auth


def _ensure_saved_account_session(account_session: TmsSession, auth_options: AuthOptions, args) -> None:
    if _backend_for_command(args) == OperationBackend.PLAYWRIGHT:
        account_session.ensure_saved_browser_authenticated(auth_options.session_dir, headless=auth_options.headless)
        return
    account_session.ensure_authenticated(auth_options)


def _login_account_with_requests(config: AccountsLoginConfig, account: AccountLoginConfig, args) -> None:
    input_func = _input_func_for_args(args)
    print_func = _print_func_for_args(args)
    result = run_batch_requests_login(
        AccountsLoginConfig(
            base_url=config.base_url,
            session_root=config.session_root,
            captcha_mode=config.captcha_mode,
            concurrency=1,
            ocr=config.ocr,
            accounts=[account],
        ),
        captcha_mode=config.captcha_mode,
        concurrency=1,
        show_captcha=getattr(args, "show_captcha", False),
        transient_retries=getattr(args, "transient_retries", 3),
        transient_delay_seconds=getattr(args, "transient_delay_seconds", 2.0),
        input_func=input_func,
        print_func=print_func,
    )
    row = result.results[0] if result.results else None
    if not row or not row.success:
        status = "unknown" if row is None else row.status
        message = "" if row is None else row.message
        suffix = f": {message}" if message else ""
        raise LoginRequired(f"requests account login failed for {account.label}: {status}{suffix}")


def _login_account_with_playwright(
    config: AccountsLoginConfig,
    account: AccountLoginConfig,
    args,
    account_session: TmsSession,
) -> None:
    result = account_session.login_playwright_with_ocr(
        account=account.account,
        password=account.password,
        session_dir=account.session_dir,
        captcha_mode=config.captcha_mode,
        headless=getattr(args, "headless", False),
        transient_retries=getattr(args, "transient_retries", 3),
        transient_delay_seconds=getattr(args, "transient_delay_seconds", 2.0),
        save=not getattr(args, "no_save_session", False),
        ocr_config=config.ocr,
    )
    if not result.get("success"):
        status = result.get("status") or "unknown"
        message = result.get("message") or ""
        suffix = f": {message}" if message else ""
        raise LoginRequired(f"playwright account login failed for {account.label}: {status}{suffix}")


def _account_operation_result(
    account: AccountLoginConfig,
    success: bool,
    status: str,
    result: Any = None,
    message: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "label": account.label,
        "session_dir": account.session_dir,
        "success": success,
        "status": status,
    }
    if message:
        payload["message"] = message
    if result is not None:
        payload["result"] = result
    return payload


def _account_operation_exception(account: AccountLoginConfig, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, LoginRequired):
        status = "login_required"
    elif isinstance(exc, TransientTmsError):
        status = "transient_error"
    elif isinstance(exc, TmsError):
        status = "tms_error"
    else:
        status = "error"
    return _account_operation_result(account, success=False, status=status, message=str(exc))


def _requests_reading_diagnostic_completed(status: str) -> bool:
    return status not in {
        "endpoint_unverified",
        "watch_time_missing_token",
        "watch_time_post_failed",
        "not_started",
    }


def _print_account_operation_payload(args, results: list[dict[str, Any]]) -> int:
    payload = {"success": all(result["success"] for result in results), "results": results}
    print_payload(args, payload)
    return 0 if payload["success"] else 12


def _print_compare_account_payload(args, results: list[dict[str, Any]]) -> int:
    payload = {"success": all(result["success"] for result in results), "results": results}
    print_payload(args, payload)
    if payload["success"]:
        return 0
    statuses = {str(result.get("status") or "") for result in results}
    if statuses and statuses.issubset({"mismatch", "skipped"}):
        return 4
    return 12


def _account_scoped_default_path(path_value: str, default_path: str, label: str) -> str:
    if path_value != default_path:
        return path_value
    path = Path(default_path)
    suffix = _safe_cli_path_segment(label)
    return str(path.with_name(f"{path.stem}-{suffix}{path.suffix}"))


def _safe_cli_path_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    segment = segment.strip("._")
    return segment or "account"


def add_login_args(parser: argparse.ArgumentParser, *, basic_help: bool = False) -> None:
    parser.add_argument("--headless", action="store_true", help=_maybe_help(hidden=basic_help))
    parser.add_argument(
        "--no-login",
        action="store_true",
        help=_maybe_help("Do not open Playwright if requests is not logged in.", hidden=basic_help),
    )
    parser.add_argument(
        "--login-method",
        choices=[method.value for method in LoginMethod],
        default=LoginMethod.AUTO.value,
        help=_maybe_help(hidden=basic_help),
    )
    parser.add_argument("--session-dir", default=".tms_session", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--account", default="", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--password", default="", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--captcha", default="", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--no-save-session", action="store_true", help=_maybe_help(hidden=basic_help))
    add_transient_args(parser, basic_help=basic_help)


def add_accounts_args(parser: argparse.ArgumentParser, *, basic_help: bool = False) -> None:
    parser.add_argument(
        "--accounts",
        help=_maybe_help("Optional TOML accounts file. Runs all accounts unless --label is provided.", hidden=basic_help),
    )
    parser.add_argument("--label", help="Optional account label to select from the accounts file.")


def add_output_args(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--human", dest="cli_mode", action="store_const", const="human", help="Use interactive text mode.")
    mode.add_argument("--agent", dest="cli_mode", action="store_const", const="agent", help="Use non-interactive JSON mode.")


def add_transient_args(parser: argparse.ArgumentParser, *, basic_help: bool = False) -> None:
    parser.add_argument("--transient-retries", type=int, default=3, help=_maybe_help(hidden=basic_help))
    parser.add_argument(
        "--transient-delay",
        dest="transient_delay_seconds",
        type=float,
        default=2.0,
        help=_maybe_help(hidden=basic_help),
    )


def _set_subparser_context(
    sub: argparse._SubParsersAction,
    root: TmsRootParser,
    path_prefix: tuple[str, ...],
) -> None:
    setattr(sub, "_tms_root_parser", root)
    setattr(sub, "_tms_path_prefix", path_prefix)


def _add_group_leaf(
    sub: argparse._SubParsersAction,
    name: str,
    help_text: str,
    command: str,
    **defaults: Any,
) -> argparse.ArgumentParser:
    path_prefix = getattr(sub, "_tms_path_prefix", ())
    prog = f"{CLI_PROG} {' '.join((*path_prefix, name))}"
    parser = sub.add_parser(name, prog=prog, help=help_text, description=help_text)
    parser.set_defaults(command=command, **defaults)
    root = getattr(sub, "_tms_root_parser", None)
    if root is not None:
        root.register_leaf_parser((*path_prefix, name), parser)
    return parser


def _add_nested_leaf(sub: argparse._SubParsersAction, name: str, help_text: str) -> argparse.ArgumentParser:
    path_prefix = getattr(sub, "_tms_path_prefix", ())
    prog = f"{CLI_PROG} {' '.join((*path_prefix, name))}"
    parser = sub.add_parser(name, prog=prog, help=help_text)
    root = getattr(sub, "_tms_root_parser", None)
    if root is not None:
        root.register_leaf_parser((*path_prefix, name), parser)
    return parser


def _add_short_leaf(
    sub: argparse._SubParsersAction,
    root: TmsRootParser,
    name: str,
    help_text: str,
    command: str,
    **defaults: Any,
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, prog=f"{CLI_PROG} {name}", help=help_text, description=help_text)
    parser.set_defaults(command=command, **defaults)
    root.register_leaf_parser((name,), parser)
    return parser


def _add_short_command_parsers(sub: argparse._SubParsersAction, root: TmsRootParser) -> None:
    sign_in = _add_short_leaf(
        sub,
        root,
        "sign-in",
        "Login with the default accounts file or interactive credentials.",
        "login-requests",
        login_requests_command="login",
    )
    _add_login_requests_login_args(sign_in, basic_help=True)

    pending = _add_short_leaf(sub, root, "pending", "List pending courses.", "list", kind="pending")
    _add_list_args(pending, default_kind="pending", basic_help=True, use_default_accounts=True)

    completed = _add_short_leaf(sub, root, "completed", "List completed courses.", "list", kind="completed")
    _add_list_args(completed, default_kind="completed", basic_help=True, use_default_accounts=True)

    course = _add_short_leaf(sub, root, "course", "Inspect one course detail tree.", "inspect-course")
    _add_inspect_args(course, basic_help=True, use_default_accounts=True)

    go = _add_short_leaf(sub, root, "go", "Complete pending courses with requests first.", "run")
    _add_run_args(go, basic_help=True, use_default_accounts=True)


def _add_grouped_command_parsers(sub: argparse._SubParsersAction, root: TmsRootParser) -> None:
    auth = sub.add_parser("auth", prog=f"{CLI_PROG} auth", help="Login, saved-session, OCR, and login diagnostics.")
    root.register_group_parser(("auth",), auth)
    auth_sub = auth.add_subparsers(dest="_auth_command", required=True, metavar="<auth-command>")
    _set_subparser_context(auth_sub, root, ("auth",))
    status = _add_group_leaf(auth_sub, "status", "Show login/session status.", "status")
    _add_status_args(status)
    login = _add_group_leaf(auth_sub, "login", "Open browser login and save cookies.", "login")
    _add_browser_login_args(login)
    requests_login = _add_group_leaf(
        auth_sub,
        "requests-login",
        "Login accounts through requests with OCR/manual captcha fallback.",
        "login-requests",
        login_requests_command="login",
    )
    _add_login_requests_login_args(requests_login)
    prepare = _add_group_leaf(
        auth_sub,
        "requests-prepare",
        "Fetch a requests login token and captcha for debugging.",
        "login-requests",
        login_requests_command="prepare",
    )
    _add_login_requests_prepare_args(prepare)
    submit = _add_group_leaf(
        auth_sub,
        "requests-submit",
        "Submit one prepared requests login payload.",
        "login-requests",
        login_requests_command="submit",
    )
    _add_login_requests_submit_args(submit)
    load_state = _add_group_leaf(
        auth_sub,
        "requests-load-state",
        "Load saved requests cookies for debugging.",
        "login-requests",
        login_requests_command="load-state",
    )
    _add_login_requests_load_state_args(load_state)
    requests_auto = _add_group_leaf(
        auth_sub,
        "requests-auto",
        "Prepare, OCR, auto-submit, and optionally retry one requests login.",
        "login-requests",
        login_requests_command="auto",
    )
    _add_login_requests_auto_args(requests_auto)
    requests_probe = _add_group_leaf(
        auth_sub,
        "requests-probe-wrong-captcha",
        "Submit a wrong captcha and save redacted response metadata.",
        "login-requests",
        login_requests_command="probe-wrong-captcha",
    )
    _add_login_requests_probe_wrong_args(requests_probe)
    requests_batch = _add_group_leaf(
        auth_sub,
        "requests-batch",
        "Test requests login for multiple TOML accounts.",
        "login-requests",
        login_requests_command="batch",
    )
    _add_login_requests_batch_args(requests_batch)
    ocr = _add_group_leaf(auth_sub, "ocr-test", "Run local OCR profile comparison on one captcha image.", "ocr-sdk-test")
    _add_ocr_sdk_test_args(ocr)
    diagnostics = _add_group_leaf(auth_sub, "diagnostics", "Compare sanitized Playwright and requests login metadata.", "login-diagnostics")
    _add_login_diagnostics_args(diagnostics)
    error_probes = _add_group_leaf(
        auth_sub,
        "error-probes",
        "Collect sanitized wrong-captcha or wrong-credential observations.",
        "login-error-probes",
    )
    _add_login_error_probes_args(error_probes)

    courses = sub.add_parser("courses", prog=f"{CLI_PROG} courses", help="List courses and inspect course details.")
    root.register_group_parser(("courses",), courses)
    courses_sub = courses.add_subparsers(dest="_courses_command", required=True, metavar="<courses-command>")
    _set_subparser_context(courses_sub, root, ("courses",))
    list_parser = _add_group_leaf(courses_sub, "list", "List pending or completed courses.", "list")
    _add_list_args(list_parser, use_default_accounts=True)
    inspect = _add_group_leaf(courses_sub, "inspect", "Inspect one course detail tree.", "inspect-course")
    _add_inspect_args(inspect, use_default_accounts=True)

    diag = sub.add_parser("diag", prog=f"{CLI_PROG} diag", help="Network, reading, form, backend, and parity diagnostics.")
    root.register_group_parser(("diag",), diag)
    diag_sub = diag.add_subparsers(dest="_diag_command", required=True, metavar="<diag-command>")
    _set_subparser_context(diag_sub, root, ("diag",))
    reading = _add_group_leaf(diag_sub, "reading", "Verify requests watchTime replay for reading/video items.", "requests-reading-accumulation-diagnostics")
    _add_requests_reading_accumulation_args(reading)
    forms = _add_group_leaf(diag_sub, "forms", "Probe or submit quiz/survey forms with requests.", "requests-form-submit-diagnostics")
    _add_requests_form_submit_args(forms)
    kexam_records = _add_group_leaf(diag_sub, "kexam-records", "Read KExam records with requests.", "requests-kexam-records")
    _add_kexam_records_args(kexam_records)
    quiz_resubmit = _add_group_leaf(
        diag_sub,
        "quiz-resubmit",
        "Submit one KExam quiz through requests and verify records.",
        "requests-quiz-resubmit-diagnostics",
    )
    _add_quiz_resubmit_args(quiz_resubmit)
    network = _add_group_leaf(diag_sub, "network", "Capture sanitized browser network metadata.", "network-diagnostics")
    _add_network_args(network)
    compare = _add_group_leaf(diag_sub, "compare", "Compare requests and Playwright read-only outputs.", "compare-backends")
    _add_compare_backends_args(compare)
    parity = _add_group_leaf(diag_sub, "parity", "Generate the requests/Playwright parity report.", "requests-parity")
    _add_requests_parity_args(parity)
    reproduction = _add_group_leaf(
        diag_sub,
        "reproduction",
        "Analyze sanitized network observations for requests reproduction.",
        "analyze-requests-reproduction",
    )
    _add_reproduction_args(reproduction)
    reading_playwright = _add_group_leaf(diag_sub, "reading-playwright", "Playwright reading accumulation diagnostic.", "reading-accumulation-diagnostics")
    _add_reading_accumulation_args(reading_playwright)
    playwright_forms = _add_group_leaf(diag_sub, "playwright-forms", "Playwright form validation diagnostic.", "validate-playwright-forms")
    _add_validate_playwright_forms_args(playwright_forms)
    playwright_kexam = _add_group_leaf(diag_sub, "playwright-kexam-records", "Playwright KExam record diagnostic.", "playwright-kexam-records")
    _add_kexam_records_args(playwright_kexam)
    playwright_quiz = _add_group_leaf(
        diag_sub,
        "playwright-quiz-resubmit",
        "Playwright KExam resubmit diagnostic.",
        "playwright-quiz-resubmit-diagnostics",
    )
    _add_quiz_resubmit_args(playwright_quiz)

    bank = sub.add_parser("bank", prog=f"{CLI_PROG} bank", help="Question-bank export, probe, and reference-bank build commands.")
    root.register_group_parser(("bank",), bank)
    bank_sub = bank.add_subparsers(dest="_bank_command", required=True, metavar="<bank-command>")
    _set_subparser_context(bank_sub, root, ("bank",))
    export = _add_group_leaf(bank_sub, "export", "Export quiz/survey history metadata with requests.", "export-question-bank")
    _add_export_question_bank_args(export)
    probe = _add_group_leaf(bank_sub, "probe", "Probe completed quiz/survey history for question-bank export.", "probe-question-bank-playwright")
    _add_probe_question_bank_playwright_args(probe)
    build = _add_group_leaf(bank_sub, "build", "Build a reference question bank from historical exports.", "build-reference-question-bank")
    _add_build_reference_question_bank_args(build)


def _add_status_args(parser: argparse.ArgumentParser) -> None:
    add_output_args(parser)


def _add_browser_login_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--session-dir", default=".tms_session")
    parser.add_argument("--no-save-session", action="store_true")
    parser.add_argument("--accounts", help="Optional TOML accounts file for Playwright OCR login.")
    parser.add_argument("--label", help="Optional account label to select from --accounts.")
    parser.add_argument("--verify-courses", action="store_true", help="Query pending/completed courses after login.")
    add_transient_args(parser)
    add_output_args(parser)


def _add_login_diagnostics_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS_PATH)
    parser.add_argument("--label")
    parser.add_argument("--headless", action="store_true")
    add_output_args(parser)
    add_transient_args(parser)


def _add_login_error_probes_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS_PATH)
    parser.add_argument("--label")
    parser.add_argument("--backend", choices=LOGIN_ERROR_PROBE_BACKENDS, default="both")
    parser.add_argument("--scenarios", default="all", help="Comma-separated: wrong-captcha,wrong-credentials,all.")
    parser.add_argument("--wrong-captcha", default=DEFAULT_WRONG_CAPTCHA)
    parser.add_argument("--fake-account", default=DEFAULT_FAKE_PROBE_ACCOUNT)
    parser.add_argument("--fake-password", default=DEFAULT_FAKE_PROBE_PASSWORD)
    parser.add_argument("--headless", action="store_true")
    add_output_args(parser)
    add_transient_args(parser)


def _add_requests_parity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", help="Optional Markdown output path.")
    parser.add_argument("--observations", help="Optional sanitized network observations JSONL to include as evidence.")
    add_output_args(parser)


def _add_compare_backends_args(parser: argparse.ArgumentParser) -> None:
    add_login_args(parser)
    add_accounts_args(parser)
    parser.add_argument("--course", help="Optional course URL or id to compare in detail.")
    parser.add_argument("--detail-limit", type=int, default=1)
    parser.add_argument("--skip-pending", action="store_true")
    parser.add_argument("--skip-completed", action="store_true")
    add_output_args(parser)


def _add_reproduction_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", default=DEFAULT_NETWORK_OBSERVATIONS_PATH)
    parser.add_argument("--output", help="Optional Markdown output path.")
    add_output_args(parser)


def _add_network_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("course", help="Course URL or id.")
    parser.add_argument("--item-title", help="Activity title to inspect.")
    parser.add_argument("--item-order", type=int, help="Activity order to inspect.")
    parser.add_argument("--action", choices=NETWORK_DIAGNOSTIC_ACTIONS, default="open-only")
    parser.add_argument("--output", default=DEFAULT_NETWORK_OBSERVATIONS_PATH)
    parser.add_argument("--wait-ms", type=int, default=2000)
    add_login_args(parser)
    add_output_args(parser)


def _add_reading_accumulation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--course", help="Optional course URL or id. Omit to auto-select completed reading/video.")
    parser.add_argument("--item-title", help="Activity title to inspect when --course is provided.")
    parser.add_argument("--item-order", type=int, help="Activity order to inspect when --course is provided.")
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--course-limit", type=int, help="Maximum completed courses to inspect during auto-selection.")
    parser.add_argument("--output", help="Optional sanitized network observation JSONL output path.")
    add_login_args(parser)
    add_accounts_args(parser)
    add_output_args(parser)


def _add_requests_reading_accumulation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--course", help="Optional course URL or id. Omit to auto-select completed reading/video.")
    parser.add_argument("--item-title", help="Activity title to inspect when --course is provided.")
    parser.add_argument("--item-order", type=int, help="Activity order to inspect when --course is provided.")
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument(
        "--force-watch-time",
        action="store_true",
        help="Diagnostic-only: post watchTime even when the selected reading/video item is already passed.",
    )
    parser.add_argument("--course-limit", type=int, help="Maximum completed courses to inspect during auto-selection.")
    add_login_args(parser)
    add_accounts_args(parser)
    add_output_args(parser)


def _add_requests_form_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--course", help="Optional course URL or id. Omit to auto-select candidates.")
    item_selector = parser.add_mutually_exclusive_group()
    item_selector.add_argument("--item-title", help="Activity title to inspect.")
    item_selector.add_argument("--item-order", type=int, help="Activity order to inspect.")
    parser.add_argument(
        "--kind",
        choices=("survey", "quiz", "both"),
        default="both",
        help="Activity kind to submit. Defaults to both survey and quiz in auto-selection mode.",
    )
    parser.add_argument(
        "--scope",
        choices=("completed", "pending", "both"),
        default="completed",
        help="Course list to scan when --course is omitted.",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Inspect candidate forms without submitting quiz or survey answers.",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=1,
        help="Maximum candidates per kind to inspect in auto-selection mode; values above 1 only apply with --probe-only.",
    )
    parser.add_argument("--quiz", choices=("auto", "confirm", "skip"), default="auto")
    parser.add_argument(
        "--question-bank",
        default=None,
        help="Question bank path, 'latest', or omitted to auto-load the latest root question-bank-*.jsonl.",
    )
    add_login_args(parser)
    add_accounts_args(parser)
    add_output_args(parser)


def _add_kexam_records_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--exam-url", default=DEFAULT_KEXAM_EXAM_URL)
    parser.add_argument(
        "--exclude-unsubmitted-records",
        dest="include_unsubmitted_records",
        action="store_false",
        default=True,
    )
    add_login_args(parser)
    add_accounts_args(parser)
    add_output_args(parser)


def _add_quiz_resubmit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--course", default=DEFAULT_KEXAM_COURSE)
    parser.add_argument("--exam-url", default=DEFAULT_KEXAM_EXAM_URL)
    parser.add_argument("--quiz", choices=("auto", "confirm", "skip"), default="auto")
    parser.add_argument(
        "--question-bank",
        default=None,
        help="Question bank path, 'latest', or omitted to auto-load the latest root question-bank-*.jsonl.",
    )
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Build and report the KExam submit payload without calling confirmRecord or submitExam.",
    )
    add_login_args(parser)
    add_accounts_args(parser)
    add_output_args(parser)


def _add_login_requests_prepare_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", default=".tms_session")
    parser.add_argument("--captcha-path", default=".tms_session/captcha.jpg")
    parser.add_argument("--show-captcha", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Keep the Playwright captcha page open until Enter.")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait after opening the captcha page.")
    add_transient_args(parser)
    add_output_args(parser)


def _add_login_requests_submit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", default=".tms_session")
    parser.add_argument("--account", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--captcha", default="")
    parser.add_argument("--allow-blank", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    add_transient_args(parser)
    add_output_args(parser)


def _add_login_requests_load_state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", default=".tms_session")
    add_output_args(parser)


def _add_login_requests_login_args(parser: argparse.ArgumentParser, *, basic_help: bool = False) -> None:
    parser.add_argument("--accounts", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--label", help="Optional account label to login from the accounts file.")
    parser.add_argument("--session-dir", default=".tms_session", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--account", default="", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--password", default="", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--concurrency", type=int, help=_maybe_help(hidden=basic_help))
    parser.add_argument("--show-captcha", action="store_true", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--no-show-captcha", dest="show_captcha", action="store_false", help=_maybe_help(hidden=basic_help))
    add_transient_args(parser, basic_help=basic_help)
    add_output_args(parser)


def _add_login_requests_auto_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", help="Optional TOML accounts file. Use --label to select one account.")
    parser.add_argument("--label", help="Account label to select from --accounts, or label for direct --account/--password mode.")
    parser.add_argument("--session-dir", default=".tms_session")
    parser.add_argument("--account", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--show-captcha", action="store_true")
    add_transient_args(parser)
    add_output_args(parser)


def _add_login_requests_probe_wrong_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", default=".tms_session/probes/wrong-captcha")
    parser.add_argument("--account", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--captcha", default="0000")
    add_transient_args(parser)
    add_output_args(parser)


def _add_login_requests_batch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--accounts", default=DEFAULT_ACCOUNTS_PATH)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--show-captcha", action="store_true")
    parser.add_argument("--no-show-captcha", dest="show_captcha", action="store_false")
    add_transient_args(parser)
    add_output_args(parser)


def _add_ocr_sdk_test_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", required=True, help="Captcha image path.")
    parser.add_argument("--profiles", help="Comma-separated profiles: v6-small,v6-tiny,v5-en-mobile,v4-en-mobile.")
    parser.add_argument("--tiers", help="Legacy comma-separated PP-OCRv6 tiers: medium,small,tiny.")
    add_output_args(parser)


def _add_list_args(
    parser: argparse.ArgumentParser,
    *,
    default_kind: str | None = None,
    basic_help: bool = False,
    use_default_accounts: bool = False,
) -> None:
    if default_kind is None:
        parser.add_argument("kind", choices=("pending", "completed"))
    else:
        parser.set_defaults(kind=default_kind)
    parser.set_defaults(use_default_accounts=use_default_accounts)
    add_login_args(parser, basic_help=basic_help)
    add_accounts_args(parser, basic_help=basic_help)
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in OperationBackend],
        default=OperationBackend.REQUESTS.value,
        help=_maybe_help(hidden=basic_help),
    )
    add_output_args(parser)


def _add_inspect_args(
    parser: argparse.ArgumentParser,
    *,
    basic_help: bool = False,
    use_default_accounts: bool = False,
) -> None:
    parser.add_argument("course")
    parser.set_defaults(use_default_accounts=use_default_accounts)
    add_login_args(parser, basic_help=basic_help)
    add_accounts_args(parser, basic_help=basic_help)
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in OperationBackend],
        default=OperationBackend.REQUESTS.value,
        help=_maybe_help(hidden=basic_help),
    )
    add_output_args(parser)


def _add_probe_question_bank_playwright_args(parser: argparse.ArgumentParser) -> None:
    add_login_args(parser)
    add_accounts_args(parser)
    parser.add_argument("--backend", choices=("playwright", "requests"), default="playwright")
    parser.add_argument("--historical-quiz-bank", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--markdown")
    parser.add_argument("--include", default="quiz,survey", help="Comma-separated values: quiz,survey.")
    parser.add_argument("--source-account-label", default="", help="Optional user-provided anonymous merge label.")
    parser.add_argument("--include-unsubmitted-records", action="store_true")
    parser.add_argument("--course-limit", type=int)
    parser.add_argument("--activity-limit", type=int)
    parser.add_argument("--delay-min-ms", type=int, default=400)
    parser.add_argument("--delay-max-ms", type=int, default=1400)
    parser.add_argument("--no-random-delay", action="store_true")
    parser.add_argument("--delay-seed", type=int)
    parser.add_argument("--allow-private-export", action="store_true")
    add_output_args(parser)


def _add_validate_playwright_forms_args(parser: argparse.ArgumentParser) -> None:
    add_login_args(parser)
    add_accounts_args(parser)
    parser.add_argument("--scope", default="completed,pending", help="Comma-separated values: completed,pending.")
    parser.add_argument("--include", default="quiz,survey", help="Comma-separated values: quiz,survey.")
    parser.add_argument("--output", default=DEFAULT_VALIDATION_JSONL_PATH)
    parser.add_argument("--markdown", default=DEFAULT_VALIDATION_MARKDOWN_PATH)
    parser.add_argument("--course-limit", type=int)
    parser.add_argument("--activity-limit", type=int)
    parser.add_argument("--include-unsubmitted-records", action="store_true")
    add_output_args(parser)


def _add_export_question_bank_args(parser: argparse.ArgumentParser) -> None:
    add_login_args(parser)
    add_accounts_args(parser)
    parser.add_argument("--output", default=DEFAULT_EXPORT_PATH)
    parser.add_argument("--format", choices=("jsonl",), default="jsonl")
    parser.add_argument("--include", default="quiz,survey", help="Comma-separated values: quiz,survey.")
    parser.add_argument("--source-account-label", default="", help="Optional user-provided anonymous merge label.")
    parser.add_argument("--allow-private-export", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--delay-min-ms", type=int, default=400)
    parser.add_argument("--delay-max-ms", type=int, default=1400)
    parser.add_argument("--no-random-delay", action="store_true")
    parser.add_argument("--delay-seed", type=int)
    add_output_args(parser)


def _add_build_reference_question_bank_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--history", default=".tms_private_exports/question-bank-history.jsonl")
    parser.add_argument("--output", default=DEFAULT_REFERENCE_BANK_JSONL_PATH)
    parser.add_argument("--markdown", default=DEFAULT_REFERENCE_BANK_MARKDOWN_PATH, help="Optional Markdown summary path. Omitted by default.")
    parser.add_argument("--ai-suggestions-jsonl")
    parser.add_argument("--posttest-ai-policy", choices=("trusted", "disabled"), default="trusted")
    add_output_args(parser)


def _add_run_args(
    parser: argparse.ArgumentParser,
    *,
    basic_help: bool = False,
    use_default_accounts: bool = False,
) -> None:
    parser.set_defaults(use_default_accounts=use_default_accounts)
    add_login_args(parser, basic_help=basic_help)
    add_accounts_args(parser, basic_help=basic_help)
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in OperationBackend],
        default=OperationBackend.REQUESTS.value,
        help=_maybe_help(
            "Execution backend. Defaults to requests; use hybrid/playwright only for debugging or fallback.",
            hidden=basic_help,
        ),
    )
    parser.add_argument("--concurrency", type=int, default=4, help=_maybe_help("Course-level worker count. Defaults to 4.", hidden=basic_help))
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=8,
        help=_maybe_help("Upper bound for adaptive course workers. Defaults to 8.", hidden=basic_help),
    )
    parser.add_argument("--no-adaptive", action="store_true", help=_maybe_help("Disable adaptive worker sizing.", hidden=basic_help))
    parser.add_argument("--survey", choices=("neutral", "skip"), default="neutral", help="Survey policy. Defaults to neutral.")
    parser.add_argument("--quiz", choices=("auto", "confirm", "skip"), default="confirm", help="Quiz policy. Defaults to confirm.")
    parser.add_argument(
        "--question-bank",
        default=None,
        help=_maybe_help(
            "Question bank path, 'latest', or omitted to auto-load the latest root question-bank-*.jsonl.",
            hidden=basic_help,
        ),
    )
    parser.add_argument("--export-question-bank-snippets", action="store_true", help=_maybe_help(hidden=basic_help))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-wait-seconds", type=int, help=_maybe_help("Optional cap for reading/video waiting.", hidden=basic_help))
    add_output_args(parser)


def ensure_for_command(session: TmsSession, args) -> AuthOptions:
    session.use_backend(_backend_for_command(args))
    if hasattr(session, "browser_headless"):
        session.browser_headless = bool(getattr(args, "headless", False))
    session.configure_transient_policy(
        getattr(args, "transient_retries", None),
        getattr(args, "transient_delay_seconds", None),
    )
    auth_options = auth_options_from_args(args)
    if getattr(args, "no_login", False):
        status = session.is_logged_in()
        if status.logged_in:
            return auth_options
        raise LoginRequired(status.message)
    try:
        session.ensure_authenticated(auth_options)
    except LoginRequired:
        if not _can_prompt_for_missing_credentials(args, auth_options):
            raise
        account, password = _prompt_credentials_for_args(args, reason="No saved session or CLI credentials were available.")
        auth_options.account = account
        auth_options.password = password
        session.ensure_authenticated(auth_options)
    return auth_options


def _backend_for_command(args) -> OperationBackend:
    backend = getattr(args, "backend", None)
    if backend is not None:
        return OperationBackend(backend)
    if getattr(args, "command", None) in PLAYWRIGHT_BACKEND_COMMANDS:
        return OperationBackend.PLAYWRIGHT
    return OperationBackend.REQUESTS


def auth_options_from_args(args) -> AuthOptions:
    return AuthOptions(
        login_method=LoginMethod(getattr(args, "login_method", LoginMethod.AUTO.value)),
        session_dir=getattr(args, "session_dir", ".tms_session"),
        account=getattr(args, "account", ""),
        password=getattr(args, "password", ""),
        captcha=getattr(args, "captcha", ""),
        captcha_mode=getattr(args, "captcha_mode", "paddleocr-sdk") or "paddleocr-sdk",
        save_session=not getattr(args, "no_save_session", False),
        headless=getattr(args, "headless", False),
        transient_retries=getattr(args, "transient_retries", 3),
        transient_delay_seconds=getattr(args, "transient_delay_seconds", 2.0),
    )


def _can_prompt_for_missing_credentials(args, options: AuthOptions) -> bool:
    if options.account and options.password:
        return False
    if options.login_method == LoginMethod.SAVED:
        return False
    if getattr(args, "no_login", False):
        return False
    return _interactive_allowed(args)


def _prompt_credentials_for_args(args, reason: str = "") -> tuple[str, str]:
    if not _interactive_allowed(args):
        raise LoginRequired("interactive credentials require human mode with TTY stdin; pass --account/--password or --accounts")
    if reason:
        _stderr_print(f"Warning: {reason}")
    account = str(getattr(args, "account", "") or "").strip()
    password = str(getattr(args, "password", "") or "")
    if not account:
        account = _stderr_input("TMS account: ").strip()
    if not password:
        password = getpass.getpass("TMS password: ", stream=sys.stderr)
    if not account or not password:
        raise LoginRequired("interactive credentials were not provided")
    return account, password


def _interactive_allowed(args) -> bool:
    return _resolved_cli_mode(args) == "human" and sys.stdin.isatty()


def _input_func_for_args(args):
    if not _interactive_allowed(args):
        return _noninteractive_input
    return _stderr_input


def _print_func_for_args(args):
    return _stderr_print


def _resolved_cli_mode(args) -> str:
    mode = getattr(args, "cli_mode", None)
    if mode in {"human", "agent"}:
        return mode
    return "human" if sys.stdin.isatty() and sys.stdout.isatty() else "agent"


def _output_json(args) -> bool:
    return _resolved_cli_mode(args) == "agent"


def print_payload(args, payload: Any) -> None:
    print_json_or_text(_output_json(args), payload)


def print_json_or_text(as_json: bool, payload: Any) -> None:
    if as_json:
        safe_print_json(to_jsonable(payload), indent=2)
        return
    text = format_text_payload(payload)
    if text:
        print(text)
        return
    safe_print_json(to_jsonable(payload), indent=2)


def format_text_payload(payload: Any) -> str:
    data = to_jsonable(payload)
    if isinstance(data, list):
        return _format_text_list(data)
    if isinstance(data, dict):
        return _format_text_mapping(data)
    if data is None:
        return ""
    return str(data)


def _format_text_list(rows: list[Any]) -> str:
    if not rows:
        return "No records."
    lines = []
    for index, row in enumerate(rows, start=1):
        if isinstance(row, dict):
            title = row.get("title") or row.get("label") or row.get("name") or f"item {index}"
            state = row.get("state") or row.get("status")
            progress = row.get("progress") or row.get("result")
            parts = [str(title)]
            if progress:
                parts.append(str(progress))
            if state:
                parts.append(str(state))
            lines.append("- " + " | ".join(parts))
        else:
            lines.append(f"- {row}")
    return "\n".join(lines)


def _format_text_mapping(data: dict[str, Any]) -> str:
    if "state" in data and set(data).issubset({"state", "message", "url", "logged_in"}):
        message = data.get("message")
        return f"{data['state']}: {message}" if message else str(data["state"])
    if "success" in data and "results" in data and isinstance(data["results"], list):
        return _format_account_operation_text(data)
    if "title" in data and isinstance(data.get("items"), list):
        return _format_course_detail_text(data)
    if _looks_like_run_result(data):
        return _format_run_result_text(data)
    status = data.get("status") or data.get("state") or data.get("error")
    if "success" in data or status:
        pieces = []
        if "success" in data:
            pieces.append("success" if data.get("success") else "failed")
        if status:
            pieces.append(str(status))
        if data.get("message"):
            pieces.append(str(data["message"]))
        return ": ".join(pieces)
    return ""


def _looks_like_run_result(data: dict[str, Any]) -> bool:
    return bool({"course_runs", "summary", "item_results"}.intersection(data))


def _format_account_operation_text(data: dict[str, Any]) -> str:
    header = "success" if data.get("success") else "failed"
    lines = [header]
    for row in data.get("results", []):
        if not isinstance(row, dict):
            lines.append(f"- {row}")
            continue
        label = row.get("label") or "account"
        status = row.get("status") or ("success" if row.get("success") else "failed")
        message = row.get("message")
        line = f"- {label}: {status}"
        if message:
            line += f" - {message}"
        lines.append(line)
    return "\n".join(lines)


def _format_course_detail_text(data: dict[str, Any]) -> str:
    lines = [str(data.get("title") or "Course detail")]
    if data.get("url"):
        lines.append(str(data["url"]))
    items = data.get("items") or []
    lines.append(f"Items: {len(items)}")
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"- {item}")
            continue
        order = item.get("order")
        prefix = f"{order}. " if order not in {None, ""} else "- "
        title = item.get("title") or "item"
        kind = item.get("kind")
        state = item.get("state")
        result = item.get("result")
        details = " | ".join(str(value) for value in (kind, state, result) if value)
        lines.append(f"{prefix}{title}" + (f" | {details}" if details else ""))
    return "\n".join(lines)


def _format_run_result_text(data: dict[str, Any]) -> str:
    success = data.get("success")
    state = data.get("state") or data.get("status")
    lines = ["Complete result: " + ("success" if success else "failed" if success is False else str(state or "done"))]
    summary = data.get("summary")
    if isinstance(summary, dict):
        summary_parts = [f"{key}={value}" for key, value in summary.items() if isinstance(value, (str, int, float, bool))]
        if summary_parts:
            lines.append("Summary: " + ", ".join(summary_parts))
    course_runs = data.get("course_runs")
    if isinstance(course_runs, list):
        for course in course_runs:
            if not isinstance(course, dict):
                continue
            title = course.get("title") or course.get("course_title") or course.get("course_id") or "course"
            status = course.get("status") or course.get("state")
            lines.append(f"- {title}" + (f": {status}" if status else ""))
    return "\n".join(lines)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_print_json(payload: Any, indent: int | None = None) -> None:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=indent))
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=indent))


def _stderr_print(message: str) -> None:
    print(message, file=sys.stderr)


def _stderr_input(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().rstrip("\n")


def _noninteractive_input(prompt: str) -> str:
    return ""


def _write_wrong_captcha_probe(session_dir: str, result: RequestsLoginResult) -> str:
    path = Path(session_dir) / "wrong_captcha_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = to_jsonable({"result": result})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return redact_sensitive_value({key: to_jsonable(item) for key, item in asdict(value).items()})
    if isinstance(value, list):
        return redact_sensitive_value([to_jsonable(item) for item in value])
    if isinstance(value, tuple):
        return redact_sensitive_value([to_jsonable(item) for item in value])
    if isinstance(value, dict):
        return redact_sensitive_value({str(key): to_jsonable(item) for key, item in value.items()})
    return redact_sensitive_value(value)


if __name__ == "__main__":
    raise SystemExit(main())
