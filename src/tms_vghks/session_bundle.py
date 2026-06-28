from __future__ import annotations

from pathlib import Path
from typing import Any

from .requests_login import (
    DEFAULT_SESSION_DIR,
    load_session_bundle as load_cookie_bundle,
    save_session_bundle as save_cookie_bundle,
)


def save_session_bundle_for_session(session: Any, path: str | Path = DEFAULT_SESSION_DIR) -> dict[str, str]:
    if session.context is not None:
        session.sync_cookies_to_requests()
    return save_cookie_bundle(session.http.cookies, session.base_url, path)


def load_session_bundle_for_session(session: Any, path: str | Path = DEFAULT_SESSION_DIR) -> dict[str, str]:
    session.http.cookies.clear()
    session.http.cookies.update(load_cookie_bundle(path))
    if session.context is not None:
        session.sync_cookies_to_browser()
    session_path = Path(path)
    return {
        "requests_cookies_path": str(session_path / "requests_cookies.json"),
        "playwright_storage_state_path": str(session_path / "playwright_storage_state.json"),
    }


__all__ = ["load_session_bundle_for_session", "save_session_bundle_for_session"]
