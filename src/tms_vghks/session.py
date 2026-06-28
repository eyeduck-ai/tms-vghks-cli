from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from http.cookiejar import Cookie
from pathlib import Path

import requests
from requests import Response
from requests.exceptions import RequestException, Timeout
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import (
    AuthOptions,
    CourseDetail,
    CourseSummary,
    LoginMethod,
    LoginStatus,
    OperationBackend,
    RequestsLoginChallenge,
    RequestsLoginResult,
    SiteState,
)
from .captcha_recognizers import OcrConfig, recognize_captcha, validate_captcha_mode
from .parsers import (
    BASE_URL,
    COMPLETED_PATH,
    PENDING_PATH,
    absolute_url,
    classify_response,
    is_login_url,
    normalize_text,
    parse_course_detail_html,
    parse_course_list_html,
)
from .requests_login import (
    DEFAULT_SESSION_DIR,
    LOGIN_NEXT_PATH,
    ajax_html_headers,
    ajax_login_headers,
    build_login_payload,
    classify_login_response,
    extract_login_failure_message,
    extract_multi_login_modal_url,
    load_challenge,
    login_response_text_excerpt,
    missing_login_fields,
    parse_login_challenge_html,
    parse_multi_login_modal_html,
    response_set_cookie_names,
    save_challenge,
    serialize_cookiejar,
    stable_login_headers,
)
from .session_browser import start_browser_for_session
from .session_bundle import load_session_bundle_for_session, save_session_bundle_for_session
from .transient import has_transient_marker, response_has_text_body, transient_message


class TmsError(RuntimeError):
    pass


class LoginRequired(TmsError):
    pass


class TransientTmsError(TmsError):
    pass


OCR_LOGIN_ATTEMPTS = 3
LOGIN_URL = "/index/login?next=%2Fcourse%2FnotCompleteList"


def _ocr_captcha_filename(attempt_index: int) -> str:
    if attempt_index <= 0:
        return "captcha.jpg"
    if attempt_index == 1:
        return "captcha_retry.jpg"
    return f"captcha_retry_{attempt_index + 1}.jpg"


class TmsSession:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: tuple[float, float] = (5.0, 30.0),
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TMS-VGHKS/0.1",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": user_agent})
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.browser_headless = False
        self.transient_retries = 3
        self.transient_delay_seconds = 2.0
        self.active_backend = OperationBackend.REQUESTS

    def url(self, path_or_url: str) -> str:
        return absolute_url(path_or_url, self.base_url) or self.base_url

    def configure_transient_policy(self, retries: int | None = None, delay_seconds: float | None = None) -> None:
        if retries is not None:
            self.transient_retries = max(0, int(retries))
        if delay_seconds is not None:
            self.transient_delay_seconds = max(0.0, float(delay_seconds))

    @property
    def backend(self) -> OperationBackend:
        return self.active_backend

    def use_backend(self, backend: OperationBackend | str) -> "TmsSession":
        self.active_backend = OperationBackend(backend)
        return self

    @contextmanager
    def using_backend(self, backend: OperationBackend | str):
        previous = self.active_backend
        self.use_backend(backend)
        try:
            yield self
        finally:
            self.active_backend = previous

    @property
    def tools(self):
        return self.backend_tools()

    def backend_tools(self, backend: OperationBackend | str | None = None):
        from .backends import backend_tools_for_session

        return backend_tools_for_session(self, self._resolve_backend(backend))

    def requests_tools(self):
        return self.backend_tools(OperationBackend.REQUESTS)

    def playwright_tools(self):
        return self.backend_tools(OperationBackend.PLAYWRIGHT)

    def hybrid_tools(self):
        return self.backend_tools(OperationBackend.HYBRID)

    @retry(
        retry=retry_if_exception_type((Timeout,)),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        stop=stop_after_attempt(2),
        reraise=True,
    )
    def request(self, method: str, path_or_url: str, **kwargs) -> Response:
        kwargs.setdefault("timeout", self.timeout)
        return self.http.request(method, self.url(path_or_url), **kwargs)

    def get(self, path_or_url: str, **kwargs) -> Response:
        return self.request("GET", path_or_url, **kwargs)

    def is_logged_in(self, fallback_browser: bool = False) -> LoginStatus:
        try:
            response = self.get(PENDING_PATH, allow_redirects=False)
        except Timeout as exc:
            return LoginStatus(SiteState.TIMEOUT, self.url(PENDING_PATH), 0, str(exc))
        except RequestException as exc:
            return LoginStatus(SiteState.UNREACHABLE, self.url(PENDING_PATH), None, str(exc))

        status = classify_response(
            response.status_code,
            response.url,
            response.headers,
            response.text if response.status_code == 200 else "",
        )
        if status.state != SiteState.UNKNOWN or not fallback_browser:
            return status
        if self.context is None:
            return status
        return self.browser_status()

    def ensure_login(
        self,
        headless: bool = False,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 2.0,
        transient_retries: int | None = None,
        transient_delay_seconds: float | None = None,
    ) -> LoginStatus:
        self.configure_transient_policy(transient_retries, transient_delay_seconds)
        status = self.is_logged_in()
        if status.logged_in:
            if self.context is not None:
                self.sync_cookies_to_browser()
            return status

        self.start_browser(headless=headless)
        assert self.page is not None
        self.sync_cookies_to_browser()
        self._goto_page(self.page, LOGIN_URL)
        self.recover_transient_page(self.page, LOGIN_URL)
        browser_status = self.browser_status()
        if browser_status.logged_in:
            self.sync_cookies_to_requests()
            return browser_status

        print(
            "目前 TMS 需要登入。請你在 Playwright 瀏覽器視窗中自行輸入員工卡號、密碼與驗證碼，並按「登入」。"
        )
        print("登入完成後程式會自動偵測待修課程頁面；請勿把帳號、密碼、驗證碼貼到終端機。")

        deadline = time.monotonic() + timeout_seconds
        last_status = browser_status
        while time.monotonic() < deadline:
            try:
                if self.page.is_closed():
                    raise TmsError("browser page was closed before login completed")
                self.recover_transient_page(self.page, LOGIN_URL if is_login_url(self.page.url) else self.page.url)
                if is_login_url(self.page.url):
                    time.sleep(poll_interval_seconds)
                    continue
                self._goto_page(self.page, PENDING_PATH)
                self.recover_transient_page(self.page, PENDING_PATH)
                last_status = self.browser_status()
                if last_status.logged_in:
                    self.sync_cookies_to_requests()
                    return last_status
                if last_status.state == SiteState.TRANSIENT_ERROR:
                    raise TransientTmsError(last_status.message)
            except Exception as exc:  # Playwright raises its own TimeoutError type.
                if isinstance(exc, TransientTmsError):
                    raise
                last_status = LoginStatus(SiteState.UNKNOWN, self.page.url if self.page else None, None, str(exc))
            time.sleep(poll_interval_seconds)
        raise LoginRequired("TMS login did not complete before timeout")

    def ensure_authenticated(self, options: AuthOptions | None = None) -> LoginStatus:
        options = options or AuthOptions()
        self.configure_transient_policy(options.transient_retries, options.transient_delay_seconds)
        method = LoginMethod(options.login_method)

        if method == LoginMethod.SAVED:
            self.load_session_bundle(options.session_dir)
            saved_status = self.is_logged_in()
            if saved_status.logged_in:
                return saved_status
            raise LoginRequired("saved TMS session is missing, expired, or not logged in")

        if method == LoginMethod.REQUESTS:
            return self._ensure_requests_authenticated(options)

        current = self.is_logged_in()
        if current.logged_in:
            if self.context is not None:
                self.sync_cookies_to_browser()
            if options.save_session:
                self.save_session_bundle(options.session_dir)
            return current

        if method == LoginMethod.AUTO:
            try:
                self.load_session_bundle(options.session_dir)
                saved_status = self.is_logged_in()
                if saved_status.logged_in:
                    return saved_status
            except TmsError:
                pass
            if self.active_backend == OperationBackend.PLAYWRIGHT:
                return self._ensure_playwright_authenticated(options)
            if self.active_backend == OperationBackend.HYBRID:
                try:
                    return self._ensure_requests_authenticated(options)
                except TransientTmsError:
                    raise
                except TmsError:
                    return self._ensure_playwright_authenticated(options)
            return self._ensure_requests_authenticated(options)

        if method == LoginMethod.PLAYWRIGHT:
            return self._ensure_playwright_authenticated(options)

        raise TmsError(f"unsupported login method: {method}")

    def ensure_saved_browser_authenticated(
        self,
        session_dir: str | Path = DEFAULT_SESSION_DIR,
        headless: bool = False,
    ) -> LoginStatus:
        self.load_session_bundle(session_dir)
        self.start_browser(headless=headless)
        assert self.page is not None
        self.sync_cookies_to_browser()
        self._goto_page(self.page, PENDING_PATH)
        self.recover_transient_page(self.page, PENDING_PATH)
        status = self.browser_status()
        if not status.logged_in:
            raise LoginRequired("saved browser TMS session is missing, expired, or not logged in")
        self.sync_cookies_to_requests()
        return status

    def _ensure_playwright_authenticated(self, options: AuthOptions) -> LoginStatus:
        if options.account and options.password and options.captcha_mode != "manual":
            result = self.login_playwright_with_ocr(
                account=options.account,
                password=options.password,
                session_dir=options.session_dir,
                captcha_mode=options.captcha_mode,
                headless=options.headless,
                timeout_seconds=options.timeout_seconds,
                transient_retries=options.transient_retries,
                transient_delay_seconds=options.transient_delay_seconds,
                save=options.save_session,
            )
            if not result.get("success"):
                raise LoginRequired(f"playwright login failed: {result.get('status')}: {result.get('message')}")
            status = self.is_logged_in(fallback_browser=True)
            if not status.logged_in:
                raise LoginRequired("playwright login completed without an authenticated TMS session")
            return status
        status = self.ensure_login(
            headless=options.headless,
            timeout_seconds=options.timeout_seconds,
            poll_interval_seconds=options.poll_interval_seconds,
            transient_retries=options.transient_retries,
            transient_delay_seconds=options.transient_delay_seconds,
        )
        if status.logged_in and options.save_session:
            self.save_session_bundle(options.session_dir)
        return status

    def _ensure_requests_authenticated(self, options: AuthOptions) -> LoginStatus:
        validate_captcha_mode(options.captcha_mode)
        result = self.submit_requests_login(
            account=options.account,
            password=options.password,
            captcha=options.captcha,
            save=options.save_session,
            session_dir=options.session_dir,
            transient_retries=options.transient_retries,
            transient_delay_seconds=options.transient_delay_seconds,
        )
        if result.status in {"missing_challenge", "missing_fields", "captcha_failed"} and self._can_retry_requests_ocr(options):
            result = self._submit_requests_login_with_ocr_attempts(options)
        elif result.status in {"missing_challenge", "missing_fields"} and self._can_prepare_requests_login(options):
            challenge = self.prepare_requests_login(
                captcha_path=Path(options.session_dir) / "captcha.jpg",
                show_captcha=options.show_captcha,
                session_dir=options.session_dir,
            )
            result = self.submit_requests_login(
                account=options.account,
                password=options.password,
                captcha=self._captcha_for_requests_login(options, challenge),
                challenge=challenge,
                save=options.save_session,
                session_dir=options.session_dir,
                transient_retries=options.transient_retries,
                transient_delay_seconds=options.transient_delay_seconds,
            )
        if not result.success:
            if result.status == "transient_error":
                raise TransientTmsError(result.message)
            raise LoginRequired(f"requests login failed: {result.status}: {result.message}")
        status = self.is_logged_in()
        if not status.logged_in:
            raise LoginRequired("requests login completed without an authenticated TMS session")
        if self.context is not None:
            self.sync_cookies_to_browser()
        return status

    def _can_prepare_requests_login(self, options: AuthOptions) -> bool:
        return bool(options.account and options.password)

    def _can_retry_requests_ocr(self, options: AuthOptions) -> bool:
        return bool(options.account and options.password and not options.captcha and options.captcha_mode != "manual")

    def _submit_requests_login_with_ocr_attempts(self, options: AuthOptions) -> RequestsLoginResult:
        last_result = RequestsLoginResult(
            success=False,
            status="manual_captcha_required",
            message="OCR captcha did not produce a successful login; manual captcha is required",
        )
        for attempt_index in range(OCR_LOGIN_ATTEMPTS):
            challenge = self.prepare_requests_login(
                captcha_path=Path(options.session_dir) / _ocr_captcha_filename(attempt_index),
                show_captcha=options.show_captcha,
                session_dir=options.session_dir,
            )
            try:
                captcha = self._captcha_for_requests_login(options, challenge)
            except LoginRequired as exc:
                return RequestsLoginResult(False, "manual_captcha_required", str(exc))
            last_result = self.submit_requests_login(
                account=options.account,
                password=options.password,
                captcha=captcha,
                challenge=challenge,
                save=options.save_session,
                session_dir=options.session_dir,
                transient_retries=options.transient_retries,
                transient_delay_seconds=options.transient_delay_seconds,
            )
            if last_result.success or last_result.status != "captcha_failed":
                return last_result
        return RequestsLoginResult(
            success=False,
            status="manual_captcha_required",
            message=f"OCR captcha failed {OCR_LOGIN_ATTEMPTS} times; manual captcha is required",
            redirect_url=last_result.redirect_url,
            response_status_code=last_result.response_status_code,
            response_json=last_result.response_json,
            response_text=last_result.response_text,
            response_text_excerpt=last_result.response_text_excerpt,
            login_state_after_post=last_result.login_state_after_post,
            failure_message=last_result.failure_message,
            set_cookie_names=last_result.set_cookie_names,
        )

    def _captcha_for_requests_login(
        self,
        options: AuthOptions,
        challenge: RequestsLoginChallenge,
    ) -> str:
        if options.captcha:
            return options.captcha
        if options.captcha_mode == "manual":
            return ""
        captcha_path = challenge.captcha_path or str(Path(options.session_dir) / "captcha.jpg")
        try:
            return recognize_captcha(captcha_path, OcrConfig(), options.captcha_mode).text
        except Exception as exc:
            raise LoginRequired(f"manual_captcha_required: requests OCR captcha failed: {exc}") from exc

    def login_playwright_with_ocr(
        self,
        account: str,
        password: str,
        session_dir: str | Path = DEFAULT_SESSION_DIR,
        captcha_mode: str = "paddleocr-sdk",
        headless: bool = False,
        timeout_seconds: int = 300,
        transient_retries: int | None = None,
        transient_delay_seconds: float | None = None,
        save: bool = True,
        ocr_config: OcrConfig | None = None,
    ) -> dict[str, object]:
        validate_captcha_mode(captcha_mode)
        if captcha_mode == "manual":
            raise TmsError("Playwright account login requires captcha_mode = paddleocr-sdk")
        ocr_config = ocr_config or OcrConfig()
        self.configure_transient_policy(transient_retries, transient_delay_seconds)
        session_path = Path(session_dir)
        captcha_path = session_path / "captcha.jpg"
        session_path.mkdir(parents=True, exist_ok=True)

        self.start_browser(headless=headless)
        assert self.page is not None
        page = self.page
        self._goto_page(page, LOGIN_URL)
        self.recover_transient_page(page, LOGIN_URL)
        status = self.browser_status()
        if status.logged_in:
            self.sync_cookies_to_requests()
            saved_paths = self.save_session_bundle(session_path) if save else {}
            return _playwright_login_payload(
                True,
                str(status.state),
                status.message or "already logged in",
                session_path,
                saved_paths,
            )

        overall_deadline = time.monotonic() + timeout_seconds
        last_status = LoginStatus(SiteState.UNKNOWN, page.url, None, "login status was not checked")
        last_captcha_path = captcha_path
        last_ocr_result = None
        handled_multi_login_modal = False
        for attempt_index in range(OCR_LOGIN_ATTEMPTS):
            if attempt_index:
                self._goto_page(page, LOGIN_URL)
                self.recover_transient_page(page, LOGIN_URL)
            last_captcha_path = session_path / _ocr_captcha_filename(attempt_index)
            captcha_locator = _first_visible_locator(
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
            captcha_locator.screenshot(path=str(last_captcha_path))
            try:
                last_ocr_result = recognize_captcha(last_captcha_path, ocr_config, captcha_mode)
            except Exception as exc:
                return _playwright_login_payload(
                    False,
                    "manual_captcha_required",
                    f"Playwright OCR captcha failed; manual captcha is required: {exc}",
                    session_path,
                    {},
                    captcha_path=last_captcha_path,
                )

            _first_visible_locator(page, ("input[name='account']", "#account"), "account input").fill(account)
            _first_visible_locator(page, ("input[name='password']", "#password"), "password input").fill(password)
            _first_visible_locator(page, ("input[name='captcha']", "#captcha"), "captcha input").fill(last_ocr_result.text)
            _click_login_submit(page)
            handled_multi_login_modal = _handle_multi_login_modal(page) or handled_multi_login_modal

            attempt_deadline = min(overall_deadline, time.monotonic() + max(10.0, timeout_seconds / OCR_LOGIN_ATTEMPTS))
            login_required_checks = 0
            while time.monotonic() < attempt_deadline:
                try:
                    self.recover_transient_page(page, LOGIN_URL if is_login_url(page.url) else page.url)
                    self._goto_page(page, PENDING_PATH)
                    self.recover_transient_page(page, PENDING_PATH)
                    last_status = self.browser_status()
                    if last_status.logged_in:
                        self.sync_cookies_to_requests()
                        saved_paths = self.save_session_bundle(session_path) if save else {}
                        return _playwright_login_payload(
                            True,
                            str(last_status.state),
                            last_status.message or "course page detected",
                            session_path,
                            saved_paths,
                            captcha_path=last_captcha_path,
                            ocr_source=last_ocr_result.source,
                            ocr_confidence=last_ocr_result.confidence,
                            handled_multi_login_modal=handled_multi_login_modal,
                        )
                    if last_status.state == SiteState.TRANSIENT_ERROR:
                        raise TransientTmsError(last_status.message)
                    if last_status.state == SiteState.LOGIN_REQUIRED:
                        login_required_checks += 1
                        if login_required_checks >= 3:
                            break
                    else:
                        login_required_checks = 0
                except TransientTmsError:
                    raise
                except Exception as exc:
                    last_status = LoginStatus(SiteState.UNKNOWN, page.url if page else None, None, str(exc))
                time.sleep(2.0)

        return _playwright_login_payload(
            False,
            "manual_captcha_required",
            f"OCR captcha failed {OCR_LOGIN_ATTEMPTS} times; manual captcha is required",
            session_path,
            {},
            captcha_path=last_captcha_path,
            ocr_source=getattr(last_ocr_result, "source", ""),
            ocr_confidence=getattr(last_ocr_result, "confidence", None),
            handled_multi_login_modal=handled_multi_login_modal,
        )

    def start_browser(self, headless: bool = False):
        return start_browser_for_session(self, headless, TmsError)

    def _goto_page(self, page, path_or_url: str, timeout_ms: int = 60000) -> None:
        page.goto(self.url(path_or_url), wait_until="commit", timeout=timeout_ms)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass

    def browser_page_status(self, page) -> LoginStatus:
        try:
            text = normalize_text(page.locator("body").inner_text(timeout=5000))
        except Exception as exc:
            return LoginStatus(SiteState.UNKNOWN, page.url, None, str(exc))
        return classify_response(200, page.url, {}, text)

    def browser_status(self) -> LoginStatus:
        if self.page is None:
            return LoginStatus(SiteState.UNKNOWN, None, None, "browser is not started")
        return self.browser_page_status(self.page)

    def page_has_transient_error(self, page, timeout_ms: int = 2000) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=timeout_ms)
        except Exception:
            return False
        return has_transient_marker(text)

    def dismiss_transient_dialog(self, page) -> bool:
        for label in ("確定", "關閉", "OK"):
            try:
                locator = page.get_by_role("button", name=label).first
                if locator.is_visible(timeout=1000):
                    locator.click()
                    return True
            except Exception:
                pass
            try:
                locator = page.get_by_text(label, exact=False).first
                if locator.is_visible(timeout=1000):
                    locator.click()
                    return True
            except Exception:
                pass
        return False

    def recover_transient_page(
        self,
        page,
        refresh_target: str | None = None,
        retries: int | None = None,
        delay_seconds: float | None = None,
    ) -> bool:
        if not self.page_has_transient_error(page):
            return False
        retries = self.transient_retries if retries is None else max(0, int(retries))
        delay_seconds = self.transient_delay_seconds if delay_seconds is None else max(0.0, float(delay_seconds))
        last_message = "TMS temporary error persisted"
        for attempt in range(retries + 1):
            status = self.browser_page_status(page)
            last_message = status.message or last_message
            if status.state != SiteState.TRANSIENT_ERROR and not self.page_has_transient_error(page):
                return True
            if attempt >= retries:
                break
            self.dismiss_transient_dialog(page)
            if delay_seconds:
                time.sleep(delay_seconds)
            try:
                if refresh_target:
                    self._goto_page(page, refresh_target)
                else:
                    page.reload(wait_until="commit", timeout=60000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
            except Exception as exc:
                last_message = str(exc)
        raise TransientTmsError(
            f"TMS transient error persisted after {retries} refresh attempt(s): {last_message}"
        )

    def recover_transient_requests(
        self,
        path_or_url: str = PENDING_PATH,
        retries: int | None = None,
        delay_seconds: float | None = None,
    ) -> LoginStatus:
        try:
            response = self._request_with_transient_retries(
                "GET",
                path_or_url,
                allow_redirects=False,
                retries=retries,
                delay_seconds=delay_seconds,
            )
        except TransientTmsError as exc:
            return LoginStatus(SiteState.TRANSIENT_ERROR, self.url(path_or_url), None, str(exc))
        except Timeout as exc:
            return LoginStatus(SiteState.TIMEOUT, self.url(path_or_url), 0, str(exc))
        except RequestException as exc:
            return LoginStatus(SiteState.UNREACHABLE, self.url(path_or_url), None, str(exc))
        return classify_response(
            response.status_code,
            response.url,
            response.headers,
            response.text if response.status_code == 200 else "",
        )

    def sync_cookies_to_requests(self) -> None:
        if self.context is None:
            return
        for cookie in self.context.cookies(self.base_url):
            self.http.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )

    def sync_cookies_to_browser(self) -> None:
        if self.context is None:
            return
        cookies = [_requests_cookie_to_playwright(cookie, self.base_url) for cookie in self.http.cookies]
        cookies = [cookie for cookie in cookies if cookie]
        if cookies:
            self.context.add_cookies(cookies)

    def clone_authenticated(self) -> "TmsSession":
        clone = TmsSession(
            base_url=self.base_url,
            timeout=self.timeout,
            user_agent=self.http.headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TMS-VGHKS/0.1"),
        )
        clone.http.headers.clear()
        clone.http.headers.update(self.http.headers)
        for cookie in self.http.cookies:
            clone.http.cookies.set_cookie(copy.copy(cookie))
        clone.configure_transient_policy(self.transient_retries, self.transient_delay_seconds)
        clone.use_backend(self.active_backend)
        clone.browser_headless = self.browser_headless
        return clone

    def prepare_requests_login(
        self,
        captcha_path: str | Path = f"{DEFAULT_SESSION_DIR}/captcha.jpg",
        show_captcha: bool = True,
        session_dir: str | Path = DEFAULT_SESSION_DIR,
    ) -> RequestsLoginChallenge:
        login_url = self.url(f"/index/login?next=%2Fcourse%2FnotCompleteList")
        response = self._request_with_transient_retries(
            "GET",
            login_url,
            timeout=self.timeout,
            headers=stable_login_headers(),
            allow_redirects=True,
        )
        response.raise_for_status()

        captcha_target = Path(captcha_path)
        captcha_target.parent.mkdir(parents=True, exist_ok=True)
        challenge = parse_login_challenge_html(
            response.text,
            response.url,
            self.base_url,
            str(captcha_target),
            serialize_cookiejar(self.http.cookies),
        )
        if not challenge.captcha_url:
            raise TmsError("captcha image URL was not found on the login page")

        captcha_response = self._request_with_transient_retries(
            "GET",
            challenge.captcha_url,
            timeout=self.timeout,
            headers=stable_login_headers({"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}),
            inspect_body=False,
        )
        captcha_response.raise_for_status()
        captcha_target.write_bytes(captcha_response.content)
        challenge.captcha_path = str(captcha_target)
        challenge.cookies = serialize_cookiejar(self.http.cookies)
        save_challenge(challenge, session_dir)
        if show_captcha:
            self.show_captcha_image(captcha_target)
        return challenge

    def submit_requests_login(
        self,
        account: str = "",
        password: str = "",
        captcha: str = "",
        challenge: RequestsLoginChallenge | None = None,
        save: bool = True,
        session_dir: str | Path = DEFAULT_SESSION_DIR,
        allow_blank: bool = False,
        transient_retries: int | None = None,
        transient_delay_seconds: float | None = None,
    ) -> RequestsLoginResult:
        if challenge is None:
            try:
                challenge = load_challenge(session_dir)
            except FileNotFoundError:
                return RequestsLoginResult(
                    success=False,
                    status="missing_challenge",
                    message=f"login challenge not found in {session_dir}; run auth requests-prepare first",
                )
        if challenge.cookies:
            self.http.cookies.update(load_session_cookies_from_challenge(challenge))

        missing = missing_login_fields(account, password, captcha)
        if missing and not allow_blank:
            return RequestsLoginResult(
                success=False,
                status="missing_fields",
                message=f"missing required login fields: {', '.join(missing)}",
                redirect_url=challenge.login_url,
            )

        payload = build_login_payload(challenge, account=account, password=password, captcha=captcha)
        try:
            response = self._request_with_transient_retries(
                "POST",
                challenge.action_url,
                data=payload,
                timeout=self.timeout,
                headers=ajax_login_headers(challenge.login_url),
                allow_redirects=False,
                retries=transient_retries,
                delay_seconds=transient_delay_seconds,
            )
        except TransientTmsError as exc:
            return RequestsLoginResult(
                success=False,
                status="transient_error",
                message=str(exc),
                redirect_url=challenge.login_url,
            )
        response_json = _safe_json(response)
        login_status = self.is_logged_in()
        handled_multi_login = False
        multi_login_action = ""
        multi_login_status = ""
        multi_login_response_status_code: int | None = None
        set_cookie_names = response_set_cookie_names(response)
        if not login_status.logged_in:
            multi_login_result = self._handle_requests_multi_login(
                response_json=response_json,
                challenge=challenge,
                payload=payload,
                transient_retries=transient_retries,
                transient_delay_seconds=transient_delay_seconds,
            )
            if multi_login_result is not None:
                handled_multi_login = True
                multi_login_action = "keep"
                multi_login_status = str(multi_login_result.get("status") or "")
                second_response = multi_login_result.get("response")
                if isinstance(second_response, Response):
                    response = second_response
                    response_json = _safe_json(response)
                    set_cookie_names = sorted(set(set_cookie_names) | set(response_set_cookie_names(response)))
                    multi_login_response_status_code = response.status_code
                    login_status = self.is_logged_in()
                if not login_status.logged_in and multi_login_status == "ok":
                    multi_login_status = "login_required"
        saved_paths: dict[str, str] = {}
        success = login_status.logged_in
        result_status = (
            classify_login_response(response_json, response.text, success)
            if success or not handled_multi_login or multi_login_status == "ok"
            else "multi_login_failed"
        )
        failure_message = "" if success else extract_login_failure_message(response_json, response.text)
        response_excerpt = login_response_text_excerpt(
            response.text,
            account=account,
            password=password,
            captcha=captcha,
        )
        if save and success:
            saved_paths = self.save_session_bundle(session_dir)
        return RequestsLoginResult(
            success=success,
            status=result_status,
            message=login_status.message or failure_message or _extract_login_message(response_json, response.text),
            redirect_url=response.headers.get("location") or login_status.url or response.url,
            response_status_code=response.status_code,
            response_json=response_json,
            response_text=response_excerpt,
            response_text_excerpt=response_excerpt,
            login_state_after_post=str(login_status.state),
            failure_message=failure_message,
            set_cookie_names=set_cookie_names,
            requests_cookies_path=saved_paths.get("requests_cookies_path"),
            playwright_storage_state_path=saved_paths.get("playwright_storage_state_path"),
            handled_multi_login=handled_multi_login,
            multi_login_action=multi_login_action,
            multi_login_status=multi_login_status,
            multi_login_response_status_code=multi_login_response_status_code,
        )

    def _handle_requests_multi_login(
        self,
        response_json,
        challenge: RequestsLoginChallenge,
        payload: dict[str, str],
        transient_retries: int | None = None,
        transient_delay_seconds: float | None = None,
    ) -> dict[str, object] | None:
        modal_url = extract_multi_login_modal_url(response_json, self.base_url)
        if not modal_url:
            return None
        try:
            modal_response = self._request_with_transient_retries(
                "GET",
                modal_url,
                timeout=self.timeout,
                headers=ajax_html_headers(challenge.login_url),
                allow_redirects=False,
                retries=transient_retries,
                delay_seconds=transient_delay_seconds,
            )
            modal_response.raise_for_status()
            modal = parse_multi_login_modal_html(modal_response.text, modal_url, self.base_url)
            if not modal.has_keep_login:
                return {"status": "keep_login_missing"}
            keep_payload = dict(payload)
            keep_payload["act"] = "keep"
            response = self._request_with_transient_retries(
                "POST",
                challenge.action_url,
                data=keep_payload,
                timeout=self.timeout,
                headers=ajax_login_headers(challenge.login_url),
                allow_redirects=False,
                retries=transient_retries,
                delay_seconds=transient_delay_seconds,
            )
        except TransientTmsError:
            raise
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        return {"status": "ok", "response": response}

    def save_session_bundle(self, path: str | Path = DEFAULT_SESSION_DIR) -> dict[str, str]:
        return save_session_bundle_for_session(self, path)

    def load_session_bundle(self, path: str | Path = DEFAULT_SESSION_DIR) -> dict[str, str]:
        try:
            return load_session_bundle_for_session(self, path)
        except FileNotFoundError as exc:
            raise TmsError(f"session bundle not found in {path}; run a successful auth requests-submit first") from exc

    def show_captcha_image(self, captcha_path: str | Path, wait_for_enter: bool = False) -> None:
        self.start_browser(headless=False)
        assert self.context is not None
        page = self.context.new_page()
        image_uri = Path(captcha_path).resolve().as_uri()
        page.set_content(
            f"""
            <!doctype html>
            <meta charset="utf-8">
            <title>TMS Captcha</title>
            <body style="font-family:sans-serif;padding:24px">
              <h1 style="font-size:18px">TMS captcha</h1>
              <img src="{image_uri}" style="image-rendering:auto;border:1px solid #ddd;padding:12px;max-width:100%">
              <p>{Path(captcha_path).resolve()}</p>
            </body>
            """
        )
        if wait_for_enter:
            input("Captcha image is open in Playwright. Press Enter to close the captcha view...")

    def list_pending_courses(self, backend: OperationBackend | str | None = None) -> list[CourseSummary]:
        selected = self._resolve_backend(backend)
        if selected == OperationBackend.PLAYWRIGHT:
            return self.list_pending_courses_playwright()
        if selected == OperationBackend.HYBRID:
            return self.list_pending_courses_hybrid()
        return self.list_pending_courses_requests()

    def list_completed_courses(self, backend: OperationBackend | str | None = None) -> list[CourseSummary]:
        selected = self._resolve_backend(backend)
        if selected == OperationBackend.PLAYWRIGHT:
            return self.list_completed_courses_playwright()
        if selected == OperationBackend.HYBRID:
            return self.list_completed_courses_hybrid()
        return self.list_completed_courses_requests()

    def list_pending_courses_requests(self) -> list[CourseSummary]:
        return self._list_courses_requests(PENDING_PATH, completed=False)

    def list_completed_courses_requests(self) -> list[CourseSummary]:
        return self._list_courses_requests(COMPLETED_PATH, completed=True)

    def list_pending_courses_playwright(self) -> list[CourseSummary]:
        html = self.fetch_html_with_browser(PENDING_PATH)
        return parse_course_list_html(html, self.base_url, completed=False)

    def list_completed_courses_playwright(self) -> list[CourseSummary]:
        html = self.fetch_html_with_browser(COMPLETED_PATH)
        return parse_course_list_html(html, self.base_url, completed=True)

    def list_pending_courses_hybrid(self) -> list[CourseSummary]:
        return self._list_courses_hybrid(PENDING_PATH, completed=False)

    def list_completed_courses_hybrid(self) -> list[CourseSummary]:
        return self._list_courses_hybrid(COMPLETED_PATH, completed=True)

    def _list_courses_requests(self, path: str, completed: bool) -> list[CourseSummary]:
        html = self._fetch_authenticated_html(path)
        return parse_course_list_html(html, self.base_url, completed=completed)

    def _list_courses_hybrid(self, path: str, completed: bool) -> list[CourseSummary]:
        try:
            courses = self._list_courses_requests(path, completed=completed)
            if courses or self.context is None:
                return courses
        except TmsError:
            pass
        html = self.fetch_html_with_browser(path)
        return parse_course_list_html(html, self.base_url, completed=completed)

    def get_course_detail(self, url_or_id: str, backend: OperationBackend | str | None = None) -> CourseDetail:
        selected = self._resolve_backend(backend)
        if selected == OperationBackend.PLAYWRIGHT:
            return self.get_course_detail_playwright(url_or_id)
        if selected == OperationBackend.HYBRID:
            return self.get_course_detail_hybrid(url_or_id)
        return self.get_course_detail_requests(url_or_id)

    def get_course_detail_requests(self, url_or_id: str) -> CourseDetail:
        url = self._coerce_course_url(url_or_id)
        html = self._fetch_authenticated_html(url)
        return parse_course_detail_html(html, url, self.base_url)

    def get_course_detail_hybrid(self, url_or_id: str) -> CourseDetail:
        url = self._coerce_course_url(url_or_id)
        try:
            detail = self.get_course_detail_requests(url)
            if detail.items or self.context is None:
                return detail
        except TmsError:
            pass
        html = self.fetch_html_with_browser(url)
        return parse_course_detail_html(html, url, self.base_url)

    def get_course_detail_playwright(self, url_or_id: str) -> CourseDetail:
        url = self._coerce_course_url(url_or_id)
        html = self.fetch_html_with_browser(url)
        return parse_course_detail_html(html, url, self.base_url)

    def fetch_activity_html(
        self,
        path_or_url: str,
        referer: str | None = None,
        backend: OperationBackend | str | None = None,
    ) -> str:
        selected = self._resolve_backend(backend)
        if selected == OperationBackend.PLAYWRIGHT:
            return self.fetch_activity_html_playwright(path_or_url, referer=referer)
        if selected == OperationBackend.HYBRID:
            return self.fetch_activity_html_hybrid(path_or_url, referer=referer)
        return self.fetch_activity_html_requests(path_or_url, referer=referer)

    def fetch_activity_html_hybrid(self, path_or_url: str, referer: str | None = None) -> str:
        try:
            return self.fetch_activity_html_requests(path_or_url, referer=referer)
        except TmsError:
            return self.fetch_activity_html_playwright(path_or_url, referer=referer)

    def fetch_activity_html_playwright(self, path_or_url: str, referer: str | None = None) -> str:
        return self.fetch_html_with_browser(path_or_url)

    def fetch_activity_html_requests(self, path_or_url: str, referer: str | None = None) -> str:
        headers = None
        if "/ajax/" in path_or_url:
            headers = ajax_html_headers(referer or self.url(PENDING_PATH))
        response = self._request_with_transient_retries(
            "GET",
            path_or_url,
            allow_redirects=False,
            headers=headers,
        )
        status = classify_response(
            response.status_code,
            response.url,
            response.headers,
            response.text if response_has_text_body(response.headers) else "",
        )
        if status.state == SiteState.LOGIN_REQUIRED:
            raise LoginRequired("TMS login is required")
        if status.state == SiteState.TRANSIENT_ERROR:
            raise TransientTmsError(status.message)
        if response.status_code >= 400:
            raise TmsError(f"TMS returned HTTP {response.status_code} for {path_or_url}")
        return _html_from_response_payload(response)

    def fetch_html_with_browser(self, path_or_url: str) -> str:
        self.start_browser(headless=self.browser_headless)
        assert self.page is not None
        self.sync_cookies_to_browser()
        self._goto_page(self.page, path_or_url)
        self.recover_transient_page(self.page, path_or_url)
        status = self.browser_status()
        if status.state == SiteState.LOGIN_REQUIRED:
            raise LoginRequired("TMS login is required")
        if status.state == SiteState.TRANSIENT_ERROR:
            raise TransientTmsError(status.message)
        self.sync_cookies_to_requests()
        return self.page.content()

    def _fetch_authenticated_html(self, path_or_url: str) -> str:
        try:
            response = self._request_with_transient_retries("GET", path_or_url, allow_redirects=False)
        except TransientTmsError:
            raise
        except Timeout as exc:
            raise TransientTmsError(f"TMS request timed out: {path_or_url}") from exc
        except RequestException as exc:
            raise TmsError(f"TMS request failed: {path_or_url}: {exc}") from exc

        status = classify_response(
            response.status_code,
            response.url,
            response.headers,
            response.text if response.status_code == 200 else "",
        )
        if status.state == SiteState.LOGIN_REQUIRED:
            raise LoginRequired("TMS login is required")
        if status.state == SiteState.TRANSIENT_ERROR:
            raise TransientTmsError(status.message)
        if status.state in {SiteState.TIMEOUT, SiteState.UNREACHABLE}:
            raise TmsError(status.message)
        if response.status_code >= 400:
            raise TmsError(f"TMS returned HTTP {response.status_code} for {path_or_url}")
        return response.text

    def _request_with_transient_retries(
        self,
        method: str,
        path_or_url: str,
        retries: int | None = None,
        delay_seconds: float | None = None,
        inspect_body: bool = True,
        **kwargs,
    ) -> Response:
        kwargs.setdefault("timeout", self.timeout)
        retries = self.transient_retries if retries is None else max(0, int(retries))
        delay_seconds = self.transient_delay_seconds if delay_seconds is None else max(0.0, float(delay_seconds))
        last_message = "temporary TMS error"
        for attempt in range(retries + 1):
            try:
                response = self.http.request(method, self.url(path_or_url), **kwargs)
            except Timeout as exc:
                last_message = f"TMS request timed out: {path_or_url}"
                if attempt >= retries:
                    raise TransientTmsError(last_message) from exc
                if delay_seconds:
                    time.sleep(delay_seconds)
                continue

            body = response.text if inspect_body and response_has_text_body(response.headers) else ""
            message = transient_message(status_code=response.status_code, text=body, fallback="")
            if message:
                last_message = f"{message}: {path_or_url}"
                if attempt >= retries:
                    raise TransientTmsError(last_message)
                if delay_seconds:
                    time.sleep(delay_seconds)
                continue
            return response
        raise TransientTmsError(last_message)

    def _coerce_course_url(self, url_or_id: str) -> str:
        if url_or_id.startswith("http://") or url_or_id.startswith("https://") or url_or_id.startswith("/"):
            return self.url(url_or_id)
        if url_or_id.isdigit():
            return self.url(f"/course/{url_or_id}")
        return self.url(url_or_id)

    def _resolve_backend(self, backend: OperationBackend | str | None) -> OperationBackend:
        return self.active_backend if backend is None else OperationBackend(backend)

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> "TmsSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _first_visible_locator(page, selectors: tuple[str, ...], description: str):
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


def _click_login_submit(page) -> None:
    try:
        with page.expect_response(_is_login_post_response, timeout=15000):
            triggered = page.evaluate(
                """
                () => {
                  const button = document.querySelector("#login_form [data-role='form-submit']");
                  if (!button) return false;
                  if (window.jQuery) {
                    window.jQuery(button).trigger("click");
                  } else {
                    button.click();
                  }
                  return true;
                }
                """
            )
        if triggered:
            _wait_after_login_submit(page)
            return
    except Exception:
        pass

    selectors = (
        "[data-role='form-submit']",
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('登入')",
        "text=登入",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=2000):
                with page.expect_response(_is_login_post_response, timeout=15000):
                    locator.click(timeout=10000)
                _wait_after_login_submit(page)
                return
        except Exception:
            pass
    try:
        with page.expect_response(_is_login_post_response, timeout=15000):
            page.locator("form#login_form").evaluate("form => form.submit()")
        _wait_after_login_submit(page)
        return
    except Exception as exc:
        raise TmsError(f"login submit control was not found: {exc}") from exc


def _wait_after_login_submit(page) -> None:
    for state, timeout in (("networkidle", 10000), ("domcontentloaded", 5000)):
        try:
            page.wait_for_load_state(state, timeout=timeout)
            return
        except Exception:
            pass
    time.sleep(1.0)


def _handle_multi_login_modal(page) -> bool:
    modal = page.locator("#checkMultiLogin_modal").first
    try:
        modal.wait_for(state="visible", timeout=5000)
    except Exception:
        return False
    try:
        keep_login = modal.locator(".keepLoginBtn").first
        keep_login.wait_for(state="visible", timeout=3000)
        keep_login.click(timeout=10000)
        _wait_after_login_submit(page)
        return True
    except Exception as exc:
        try:
            if not modal.is_visible(timeout=500):
                return True
        except Exception:
            return True
        raise TmsError(f"multi-login confirmation failed: {exc}") from exc
    return False


def _is_login_post_response(response) -> bool:
    try:
        return response.request.method.upper() == "POST" and "/index/login" in response.url
    except Exception:
        return False


def _playwright_login_payload(
    success: bool,
    status: str,
    message: str,
    session_dir: Path,
    saved_paths: dict[str, str],
    captcha_path: Path | None = None,
    ocr_source: str = "",
    ocr_confidence: float | None = None,
    handled_multi_login_modal: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": success,
        "status": status,
        "message": message,
        "session_dir": str(session_dir),
        "requests_cookies_path": saved_paths.get("requests_cookies_path"),
        "playwright_storage_state_path": saved_paths.get("playwright_storage_state_path"),
    }
    if captcha_path is not None:
        payload["captcha_image_path"] = str(captcha_path)
    if ocr_source:
        payload["ocr_source"] = ocr_source
    if ocr_confidence is not None:
        payload["ocr_confidence"] = ocr_confidence
    if handled_multi_login_modal:
        payload["handled_multi_login_modal"] = True
    return payload


def _requests_cookie_to_playwright(cookie: Cookie, base_url: str) -> dict | None:
    if not cookie.name:
        return None
    value: dict[str, object] = {
        "name": cookie.name,
        "value": cookie.value,
        "path": cookie.path or "/",
    }
    if cookie.domain:
        value["domain"] = cookie.domain
    else:
        value["url"] = base_url
    if cookie.expires:
        value["expires"] = cookie.expires
    return value


def load_session_cookies_from_challenge(challenge: RequestsLoginChallenge) -> requests.cookies.RequestsCookieJar:
    from .requests_login import restore_cookiejar

    return restore_cookiejar(challenge.cookies)


def _safe_json(response: Response):
    try:
        return response.json()
    except ValueError:
        return None


def _html_from_response_payload(response: Response) -> str:
    text = response.text
    payload = _safe_json(response)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("html"), str):
            return data["html"]
        if isinstance(payload.get("html"), str):
            return str(payload["html"])
    return text


def _extract_login_message(response_json, response_text: str) -> str:
    if isinstance(response_json, dict):
        ret = response_json.get("ret")
        if isinstance(ret, dict):
            for key in ("msg", "message", "error"):
                if ret.get(key):
                    return str(ret[key])
        for key in ("msg", "message", "error"):
            if response_json.get(key):
                return str(response_json[key])
    return normalize_text(response_text)[:300]
