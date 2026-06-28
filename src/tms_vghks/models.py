from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ItemKind(StrEnum):
    READING = "reading"
    VIDEO = "video"
    QUIZ = "quiz"
    SURVEY = "survey"
    UNKNOWN = "unknown"


class ItemState(StrEnum):
    PASSED = "passed"
    IN_PROGRESS = "in_progress"
    PENDING = "pending"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class SiteState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGIN_REQUIRED = "login_required"
    TRANSIENT_ERROR = "transient_error"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


class LoginMethod(StrEnum):
    AUTO = "auto"
    SAVED = "saved"
    PLAYWRIGHT = "playwright"
    REQUESTS = "requests"


class OperationBackend(StrEnum):
    HYBRID = "hybrid"
    PLAYWRIGHT = "playwright"
    REQUESTS = "requests"


@dataclass(slots=True)
class AuthOptions:
    login_method: LoginMethod = LoginMethod.AUTO
    session_dir: str = ".tms_session"
    account: str = ""
    password: str = ""
    captcha: str = ""
    captcha_mode: str = "paddleocr-sdk"
    save_session: bool = True
    headless: bool = False
    timeout_seconds: int = 300
    poll_interval_seconds: float = 2.0
    show_captcha: bool = False
    transient_retries: int = 3
    transient_delay_seconds: float = 2.0


@dataclass(slots=True)
class LoginStatus:
    state: SiteState
    url: str | None = None
    status_code: int | None = None
    message: str = ""

    @property
    def logged_in(self) -> bool:
        return self.state == SiteState.LOGGED_IN


@dataclass(slots=True)
class RequestsLoginChallenge:
    login_url: str
    action_url: str
    hidden_fields: dict[str, str]
    anticsrf: str | None = None
    captcha_url: str | None = None
    captcha_path: str | None = None
    cookies: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RequestsLoginResult:
    success: bool
    status: str
    message: str = ""
    redirect_url: str | None = None
    response_status_code: int | None = None
    response_json: dict[str, Any] | list[Any] | None = None
    response_text: str = ""
    response_text_excerpt: str = ""
    login_state_after_post: str = ""
    failure_message: str = ""
    set_cookie_names: list[str] = field(default_factory=list)
    requests_cookies_path: str | None = None
    playwright_storage_state_path: str | None = None
    handled_multi_login: bool = False
    multi_login_action: str = ""
    multi_login_status: str = ""
    multi_login_response_status_code: int | None = None


@dataclass(slots=True)
class CourseSummary:
    title: str
    detail_url: str | None = None
    course_id: str | None = None
    progress: str | None = None
    completed: bool | None = None
    raw_text: str = ""
    data_issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CourseItem:
    title: str
    order: int | None = None
    kind: ItemKind = ItemKind.UNKNOWN
    state: ItemState = ItemState.UNKNOWN
    detail_url: str | None = None
    pass_condition: str | None = None
    result: str | None = None
    passed_marker: str | None = None
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.state == ItemState.PASSED


@dataclass(slots=True)
class CourseDetail:
    title: str
    url: str
    course_id: str | None = None
    items: list[CourseItem] = field(default_factory=list)
    completed: bool = False
    blockers: list[str] = field(default_factory=list)
    raw_text: str = ""
    data_issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunResult:
    success: bool
    state: ItemState | SiteState | str
    message: str = ""
    course: CourseSummary | CourseDetail | None = None
    item: CourseItem | None = None
    data: dict[str, Any] = field(default_factory=dict)
    sanitized_question_bank_snippet: str | None = None
