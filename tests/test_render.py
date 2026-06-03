"""render.py — headless-fetch helper. Env-independent: tests inject a fake
playwright or force its absence, so they pass with OR without the `render`
extra installed (no real browser is ever launched)."""
from __future__ import annotations

import sys
import types

from digest.ingest import render


# ── fake playwright.sync_api ──────────────────────────────────────────────────

class _FakePage:
    def __init__(self, html: str, log: list):
        self._html, self._log = html, log

    def goto(self, url, wait_until=None, timeout=None):
        self._log.append(("goto", url, wait_until))

    def wait_for_selector(self, sel, timeout=None):
        self._log.append(("wait_selector", sel))

    def wait_for_timeout(self, ms):
        self._log.append(("wait_timeout", ms))

    def content(self):
        return self._html


class _FakeBrowser:
    def __init__(self, html, log):
        self._html, self._log = html, log

    def new_page(self, **kwargs):
        self._log.append(("new_page", kwargs.get("user_agent", "")[:10]))
        return _FakePage(self._html, self._log)

    def close(self):
        self._log.append(("close",))


class _FakeChromium:
    def __init__(self, html, log, launch_error=False):
        self._html, self._log, self._err = html, log, launch_error

    def launch(self, headless=True):
        if self._err:
            raise RuntimeError("Executable doesn't exist (browser not installed)")
        return _FakeBrowser(self._html, self._log)


class _FakeCtx:
    def __init__(self, html, log, launch_error=False):
        self.chromium = _FakeChromium(html, log, launch_error)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_playwright(monkeypatch, html, log, *, launch_error=False):
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _FakeCtx(html, log, launch_error)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_fetch_rendered_happy_path(monkeypatch):
    log: list = []
    _install_fake_playwright(monkeypatch, "<html>rendered</html>", log)
    out = render.fetch_rendered("https://x.test/", wait_selector=".thing")
    assert out == "<html>rendered</html>"
    kinds = [c[0] for c in log]
    assert kinds == ["new_page", "goto", "wait_selector", "close"]


def test_fetch_rendered_no_selector_settles_with_timeout(monkeypatch):
    log: list = []
    _install_fake_playwright(monkeypatch, "<html>ok</html>", log)
    out = render.fetch_rendered("https://x.test/")   # no wait_selector
    assert out == "<html>ok</html>"
    assert ("wait_timeout", 3500) in log
    assert not any(c[0] == "wait_selector" for c in log)


def test_fetch_rendered_returns_none_when_playwright_missing(monkeypatch):
    # Force `import playwright.sync_api` to fail (parent set to None in sys.modules).
    monkeypatch.delitem(sys.modules, "playwright.sync_api", raising=False)
    monkeypatch.setitem(sys.modules, "playwright", None)
    assert render.fetch_rendered("https://x.test/") is None
    assert render.render_available() is False


def test_fetch_rendered_returns_none_on_launch_error(monkeypatch):
    _install_fake_playwright(monkeypatch, "<html/>", [], launch_error=True)
    # A missing browser binary surfaces as a launch RuntimeError → graceful None.
    assert render.fetch_rendered("https://x.test/") is None
