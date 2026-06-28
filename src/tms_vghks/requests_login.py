from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.cookies import RequestsCookieJar, create_cookie

from .models import RequestsLoginChallenge
from .parsers import absolute_url, normalize_text

LOGIN_PATH = "/index/login"
LOGIN_NEXT_PATH = "/course/notCompleteList"
DEFAULT_SESSION_DIR = ".tms_session"
REQUESTS_COOKIES_FILE = "requests_cookies.json"
PLAYWRIGHT_STATE_FILE = "playwright_storage_state.json"
LOGIN_CHALLENGE_FILE = "login_challenge.json"
CAPTCHA_FAILURE_PATTERNS = ("captcha", "capcha", "驗證碼", "圖形驗證", "驗證失敗")
CREDENTIAL_FAILURE_PATTERNS = ("account", "password", "帳號", "密碼", "員工", "登入失敗", "login failed")
MULTI_LOGIN_MODAL_PATTERN = re.compile(
    r"""checkMultiLogin_modal[\s\S]*?\.data\(\s*['"]url['"]\s*,\s*(['"])(?P<url>.*?)\1\s*\)""",
    re.IGNORECASE,
)


@dataclass(slots=True)
class RequestsMultiLoginChallenge:
    modal_url: str
    form_action_url: str
    hidden_fields: dict[str, str] = field(default_factory=dict)
    has_keep_login: bool = False
    has_kick_other: bool = False


def stable_login_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if extra:
        headers.update(extra)
    return headers


def ajax_login_headers(referer: str) -> dict[str, str]:
    return stable_login_headers(
        {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
    )


def ajax_html_headers(referer: str) -> dict[str, str]:
    return stable_login_headers(
        {
            "Accept": "text/html, */*; q=0.01",
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
        }
    )


def parse_login_challenge_html(
    html: str,
    login_url: str,
    base_url: str,
    captcha_path: str | None,
    cookies: list[dict[str, Any]] | None = None,
) -> RequestsLoginChallenge:
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form", id="login_form") or soup.find("form")
    if form is None:
        raise ValueError("login form was not found")

    action_url = absolute_url(form.get("action") or LOGIN_PATH, base_url) or base_url
    hidden_fields: dict[str, str] = {}
    for input_node in form.find_all("input"):
        name = input_node.get("name")
        if not name:
            continue
        input_type = (input_node.get("type") or "").lower()
        if input_type == "hidden":
            hidden_fields[name] = input_node.get("value") or ""

    captcha_node = form.select_one("img.js-captcha") or soup.find(
        "img", src=lambda value: bool(value and ("captcha" in value.lower() or "capcha" in value.lower()))
    )
    captcha_url = absolute_url(captcha_node.get("src"), base_url) if captcha_node else None
    return RequestsLoginChallenge(
        login_url=login_url,
        action_url=action_url,
        hidden_fields=hidden_fields,
        anticsrf=hidden_fields.get("anticsrf"),
        captcha_url=captcha_url,
        captcha_path=captcha_path,
        cookies=cookies or [],
    )


def extract_multi_login_modal_url(response_json: Any, base_url: str) -> str | None:
    custom_js = _extract_custom_js(response_json)
    if not custom_js or "checkMultiLogin_modal" not in custom_js:
        return None
    match = MULTI_LOGIN_MODAL_PATTERN.search(custom_js)
    if not match:
        return None
    return absolute_url(match.group("url"), base_url)


def parse_multi_login_modal_html(
    html: str,
    modal_url: str,
    base_url: str,
) -> RequestsMultiLoginChallenge:
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form", id="categoryForm") or soup.find("form")
    if form is None:
        raise ValueError("multi-login form was not found")
    action_url = absolute_url(form.get("action") or "", base_url)
    if not action_url:
        raise ValueError("multi-login form action was not found")
    hidden_fields: dict[str, str] = {}
    for input_node in form.find_all("input"):
        name = input_node.get("name")
        if name and (input_node.get("type") or "").lower() == "hidden":
            hidden_fields[name] = input_node.get("value") or ""
    return RequestsMultiLoginChallenge(
        modal_url=modal_url,
        form_action_url=action_url,
        hidden_fields=hidden_fields,
        has_keep_login=form.select_one(".keepLoginBtn") is not None,
        has_kick_other=form.select_one(".kickOtherBtn") is not None,
    )


def build_login_payload(
    challenge: RequestsLoginChallenge,
    account: str = "",
    password: str = "",
    captcha: str = "",
) -> dict[str, str]:
    payload = dict(challenge.hidden_fields)
    payload.update(
        {
            "account": account,
            "password": password,
            "captcha": captcha,
            "_fmSubmit": "yes",
            "formVer": "3.0",
            "formId": "login_form",
        }
    )
    payload.setdefault("next", LOGIN_NEXT_PATH)
    payload.setdefault("act", "")
    return payload


def _extract_custom_js(response_json: Any) -> str:
    if not isinstance(response_json, dict):
        return ""
    ret = response_json.get("ret")
    if not isinstance(ret, dict):
        return ""
    action = ret.get("action")
    if not isinstance(action, dict):
        return ""
    custom_js = action.get("customJs")
    return custom_js if isinstance(custom_js, str) else ""


def missing_login_fields(account: str, password: str, captcha: str) -> list[str]:
    missing: list[str] = []
    if not account:
        missing.append("account")
    if not password:
        missing.append("password")
    if not captcha:
        missing.append("captcha")
    return missing


def classify_login_response(response_json: Any, response_text: str, logged_in: bool) -> str:
    if logged_in:
        return "logged_in"
    message = normalize_text(_login_response_message_text(response_json, response_text)).lower()
    if any(pattern.lower() in message for pattern in CAPTCHA_FAILURE_PATTERNS):
        return "captcha_failed"
    if any(pattern.lower() in message for pattern in CREDENTIAL_FAILURE_PATTERNS):
        return "credential_failed"
    return "login_failed"


def extract_login_failure_message(response_json: Any, response_text: str) -> str:
    return normalize_text(_login_response_message_text(response_json, response_text))[:300]


def login_response_text_excerpt(
    response_text: str,
    account: str = "",
    password: str = "",
    captcha: str = "",
    limit: int = 2000,
) -> str:
    text = response_text or ""
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for node in soup.find_all(("script", "style")):
            node.decompose()
        for node in soup.find_all("input"):
            if node.has_attr("value"):
                node["value"] = ""
        text = soup.get_text(" ", strip=True)
    text = normalize_text(text)
    for sensitive in (account, password, captcha):
        if sensitive:
            text = text.replace(sensitive, "REDACTED")
    text = re.sub(r"(?i)(anticsrf|csrf|token)\s*[=:]\s*['\"]?[^'\"\s<>&]+", r"\1=REDACTED", text)
    return text[:limit]


def response_set_cookie_names(response: requests.Response) -> list[str]:
    return sorted({cookie.name for cookie in response.cookies})


def serialize_cookiejar(cookiejar: RequestsCookieJar) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for cookie in cookiejar:
        rest = dict(getattr(cookie, "_rest", {}) or {})
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "secure": cookie.secure,
                "rest": rest,
            }
        )
    return cookies


def _login_response_message_text(response_json: Any, response_text: str) -> str:
    values: list[str] = []
    _collect_json_messages(response_json, values)
    if values:
        return " ".join(values)
    return login_response_text_excerpt(response_text, limit=2000)


def _collect_json_messages(value: Any, values: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            values.append(value.strip())
        return
    if isinstance(value, dict):
        for key in ("msg", "message", "error", "errorMsg", "retmsg", "reason"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            elif isinstance(item, (dict, list)):
                _collect_json_messages(item, values)
        for key in ("ret", "data", "result"):
            if key in value:
                _collect_json_messages(value[key], values)
        return
    if isinstance(value, list):
        for item in value:
            _collect_json_messages(item, values)


def restore_cookiejar(cookies: list[dict[str, Any]]) -> RequestsCookieJar:
    jar = RequestsCookieJar()
    for row in cookies:
        jar.set_cookie(
            create_cookie(
                name=row["name"],
                value=row.get("value") or "",
                domain=row.get("domain") or "",
                path=row.get("path") or "/",
                secure=bool(row.get("secure")),
                expires=row.get("expires"),
                rest=row.get("rest") or {},
            )
        )
    return jar


def cookiejar_to_playwright_storage_state(cookiejar: RequestsCookieJar, base_url: str) -> dict[str, Any]:
    host = urlparse(base_url).hostname or "tms.vghks.gov.tw"
    cookies: list[dict[str, Any]] = []
    for cookie in cookiejar:
        domain = cookie.domain or host
        rest = dict(getattr(cookie, "_rest", {}) or {})
        same_site = _normalize_same_site(rest.get("SameSite") or rest.get("samesite"))
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
                "expires": cookie.expires if cookie.expires is not None else -1,
                "httpOnly": "HttpOnly" in rest or "httponly" in rest,
                "secure": bool(cookie.secure),
                "sameSite": same_site,
            }
        )
    return {"cookies": cookies, "origins": []}


def save_challenge(challenge: RequestsLoginChallenge, session_dir: str | Path = DEFAULT_SESSION_DIR) -> str:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    path = session_path / LOGIN_CHALLENGE_FILE
    path.write_text(json.dumps(_challenge_to_dict(challenge), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def load_challenge(session_dir: str | Path = DEFAULT_SESSION_DIR) -> RequestsLoginChallenge:
    path = Path(session_dir) / LOGIN_CHALLENGE_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    return RequestsLoginChallenge(**data)


def save_session_bundle(
    cookiejar: RequestsCookieJar,
    base_url: str,
    session_dir: str | Path = DEFAULT_SESSION_DIR,
) -> dict[str, str]:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    requests_path = session_path / REQUESTS_COOKIES_FILE
    playwright_path = session_path / PLAYWRIGHT_STATE_FILE
    cookie_bundle = {
        "base_url": base_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cookies": serialize_cookiejar(cookiejar),
    }
    requests_path.write_text(json.dumps(cookie_bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    playwright_path.write_text(
        json.dumps(cookiejar_to_playwright_storage_state(cookiejar, base_url), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "requests_cookies_path": str(requests_path),
        "playwright_storage_state_path": str(playwright_path),
    }


def load_session_bundle(session_dir: str | Path = DEFAULT_SESSION_DIR) -> RequestsCookieJar:
    path = Path(session_dir) / REQUESTS_COOKIES_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    return restore_cookiejar(data.get("cookies", []))


def _challenge_to_dict(challenge: RequestsLoginChallenge) -> dict[str, Any]:
    return {
        "login_url": challenge.login_url,
        "action_url": challenge.action_url,
        "hidden_fields": challenge.hidden_fields,
        "anticsrf": challenge.anticsrf,
        "captcha_url": challenge.captcha_url,
        "captcha_path": challenge.captcha_path,
        "cookies": challenge.cookies,
    }


def _normalize_same_site(value: Any) -> str:
    if not value:
        return "Lax"
    lowered = str(value).lower()
    if lowered == "strict":
        return "Strict"
    if lowered == "none":
        return "None"
    return "Lax"
