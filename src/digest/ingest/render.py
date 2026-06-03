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
