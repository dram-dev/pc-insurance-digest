"""Headless-browser fetch for JS-rendered or WAF-blocked sources.

A handful of P&C sources don't yield to a plain `requests.get`: their content
is rendered client-side (TX TDI year index, LexisNexis' Algolia feed) or a WAF
403s a non-browser client (LA LDI, JD Power). This module drives a real headless
Chromium via Playwright to produce the final DOM, which the existing
BeautifulSoup selectors then parse unchanged.

Playwright is an **optional** dependency — the `render` extra — plus a one-time
browser download:

    uv sync --extra render && uv run playwright install chromium

When it isn't installed/available, `fetch_rendered` logs an actionable warning
and returns ``None``. Callers treat that exactly like a failed fetch and skip
the source, so the base install stays lean and a missing browser never crashes
a pipeline run.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Same UA the requests-based scrapers send, so a site sees a consistent client.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_SETUP_HINT = (
    "enable the render extra: `uv sync --extra render && "
    "uv run playwright install chromium`"
)


def render_available() -> bool:
    """True when the Playwright python package is importable.

    Note this does not guarantee the browser binary is installed — that surfaces
    as a launch failure inside `fetch_rendered`, which is handled the same way.
    """
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def fetch_rendered(
    url: str,
    *,
    wait_selector: str | None = None,
    wait_ms: int = 3500,
    timeout_ms: int = 35000,
    user_agent: str = _DEFAULT_UA,
) -> str | None:
    """Return the post-JS HTML of ``url``, or ``None`` if rendering is unavailable.

    Best-effort and self-contained: launches a headless Chromium, navigates,
    waits for ``wait_selector`` (falling back to a fixed ``wait_ms`` settle when
    it's absent or never appears), and returns ``page.content()``. Any failure —
    Playwright not installed, browser binary missing, navigation/timeout — is
    logged and yields ``None`` so the caller can skip the source rather than
    crash.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logger.warning("render: Playwright not installed — %s", _SETUP_HINT)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 2400},
                )
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=min(timeout_ms, 12000))
                    except Exception:
                        # Selector never appeared; return whatever rendered so the
                        # caller's own 0-node warning fires with the real page.
                        page.wait_for_timeout(wait_ms)
                else:
                    page.wait_for_timeout(wait_ms)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — missing browser binary lands here too
        logger.warning("render: failed to render %s: %s — %s", url, exc, _SETUP_HINT)
        return None


def _apply_action(page, action: tuple, step_timeout: int) -> None:
    """Execute one interactive action tuple against a Playwright page.

      ("click",    selector)         click (CSS, or Playwright text=/:has-text)
      ("type",     selector, text)   focus + type (masked/calendar inputs)
      ("fill",     selector, text)   set value via fill()
      ("select",   selector, value)  native <select> by value or label
      ("press",    key)              keyboard press, e.g. "Tab"
      ("wait",     ms)               fixed pause
      ("wait_for", selector)         wait for a selector (best-effort, never raises)
    """
    verb = action[0]
    if verb == "click":
        page.click(action[1], timeout=step_timeout)
    elif verb == "type":
        page.click(action[1], timeout=step_timeout)
        page.type(action[1], action[2], delay=35)
    elif verb == "fill":
        page.fill(action[1], action[2], timeout=step_timeout)
    elif verb == "select":
        try:
            page.select_option(action[1], value=action[2], timeout=step_timeout)
        except Exception:
            page.select_option(action[1], label=action[2], timeout=step_timeout)
    elif verb == "press":
        page.keyboard.press(action[1])
    elif verb == "wait":
        page.wait_for_timeout(action[1])
    elif verb == "wait_for":
        try:
            page.wait_for_selector(action[1], timeout=step_timeout)
        except Exception:
            pass  # proceed with whatever rendered
    else:
        logger.warning("render: unknown interactive verb %r — skipped", verb)


def fetch_rendered_interactive(
    url: str,
    actions: list[tuple],
    *,
    settle_ms: int = 4000,
    timeout_ms: int = 45000,
    user_agent: str = _DEFAULT_UA,
) -> str | None:
    """Drive a multi-step page (forms / JSF portals) and return the final HTML.

    For sources that need interaction before the data exists — e.g. SERFF Filing
    Access (Begin Search → accept agreement → pick line of business → set a date →
    Search). ``actions`` is a list of tuples (see ``_apply_action``).

    A step that fails (missing selector, timeout) aborts and returns ``None`` —
    the caller treats that like a failed fetch and skips the source. Returns
    ``None`` too when Playwright/the browser binary is unavailable.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logger.warning("render: Playwright not installed — %s", _SETUP_HINT)
        return None

    step_timeout = min(timeout_ms, 20000)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 2400},
                )
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                for action in actions:
                    _apply_action(page, action, step_timeout)
                page.wait_for_timeout(settle_ms)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("render: interactive render of %s failed: %s — %s", url, exc, _SETUP_HINT)
        return None


def fetch_rendered_paginated(
    url: str,
    actions: list[tuple],
    *,
    next_selector: str,
    ready_selector: str,
    disabled_class: str = "ui-state-disabled",
    max_pages: int = 6,
    page_settle_ms: int = 2500,
    settle_ms: int = 4000,
    timeout_ms: int = 45000,
    user_agent: str = _DEFAULT_UA,
) -> list[str]:
    """Run setup ``actions`` (e.g. a SERFF search), then walk the result pages.

    After the setup, captures the current page HTML, then clicks ``next_selector``
    and waits for ``ready_selector`` between pages — stopping when the next control
    carries ``disabled_class`` (last page), the click fails, or ``max_pages`` is
    hit. Returns the list of per-page HTML snapshots (``[]`` when Playwright/the
    browser is unavailable or the setup fails, so the caller skips the source).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logger.warning("render: Playwright not installed — %s", _SETUP_HINT)
        return []

    step_timeout = min(timeout_ms, 20000)
    pages: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 2400},
                )
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                for action in actions:
                    _apply_action(page, action, step_timeout)
                page.wait_for_timeout(settle_ms)

                for _ in range(max_pages):
                    pages.append(page.content())
                    nxt = page.query_selector(next_selector)
                    if nxt is None or disabled_class in (nxt.get_attribute("class") or ""):
                        break
                    try:
                        nxt.click(timeout=step_timeout)
                        page.wait_for_selector(ready_selector, timeout=step_timeout)
                    except Exception:
                        break
                    page.wait_for_timeout(page_settle_ms)
                return pages
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("render: paginated render of %s failed: %s — %s", url, exc, _SETUP_HINT)
        return pages
