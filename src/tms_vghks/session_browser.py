from __future__ import annotations

from typing import Any, Type


def start_browser_for_session(session: Any, headless: bool, error_cls: Type[Exception]) -> Any:
    if session.context is not None:
        return session.context
    session.browser_headless = bool(headless)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise error_cls("playwright is required for login and interactive item handling") from exc

    session._playwright = sync_playwright().start()
    session.browser = session._playwright.chromium.launch(headless=headless)
    session.context = session.browser.new_context(base_url=session.base_url, ignore_https_errors=True)
    session.sync_cookies_to_browser()
    session.page = session.context.new_page()
    return session.context


__all__ = ["start_browser_for_session"]
