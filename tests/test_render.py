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


# ── paginated fetch ───────────────────────────────────────────────────────────

class _PagedNext:
    def __init__(self, page):
        self._page = page

    def get_attribute(self, name):
        if name == "class":
            last = self._page.idx >= len(self._page.pages) - 1
            return "ui-paginator-next" + (" ui-state-disabled" if last else "")
        return None

    def click(self, timeout=None):
        self._page.idx += 1


class _PagedPage:
    def __init__(self, pages):
        self.pages, self.idx = pages, 0
        self.actions: list = []

    def goto(self, *a, **k): pass
    def content(self): return self.pages[self.idx]
    def query_selector(self, sel): return _PagedNext(self)
    def click(self, sel, **k): self.actions.append(("click", sel))
    def type(self, sel, text, **k): self.actions.append(("type", sel, text))
    def fill(self, sel, text, **k): self.actions.append(("fill", sel, text))
    def select_option(self, sel, **k): self.actions.append(("select", sel, k))
    def wait_for_selector(self, *a, **k): pass
    def wait_for_timeout(self, *a, **k): pass

    @property
    def keyboard(self):
        page = self
        class _K:
            def press(self, key): page.actions.append(("press", key))
        return _K()


def _install_paged(monkeypatch, pages):
    page = _PagedPage(pages)
    mod = types.ModuleType("playwright.sync_api")

    class _Br:
        def new_page(self, **k): return page
        def close(self): pass

    class _Ch:
        def launch(self, headless=True): return _Br()

    class _Ctx:
        chromium = _Ch()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    mod.sync_playwright = lambda: _Ctx()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    return page


def test_paginated_walks_until_next_disabled(monkeypatch):
    page = _install_paged(monkeypatch, ["<p>1</p>", "<p>2</p>", "<p>3</p>"])
    out = render.fetch_rendered_paginated(
        "https://x/", [("click", "#go"), ("select", "sel", "100")],
        next_selector=".ui-paginator-next", ready_selector="tbody tr",
        max_pages=10, page_settle_ms=0, settle_ms=0,
    )
    assert out == ["<p>1</p>", "<p>2</p>", "<p>3</p>"]      # stops on the disabled next
    # setup actions ran, incl. the new 'select' verb.
    assert ("click", "#go") in page.actions
    assert any(a[0] == "select" for a in page.actions)


def test_paginated_respects_max_pages(monkeypatch):
    _install_paged(monkeypatch, ["<p>1</p>", "<p>2</p>", "<p>3</p>", "<p>4</p>"])
    out = render.fetch_rendered_paginated(
        "https://x/", [], next_selector=".ui-paginator-next",
        ready_selector="tbody tr", max_pages=2, page_settle_ms=0, settle_ms=0,
    )
    assert out == ["<p>1</p>", "<p>2</p>"]                  # capped before the last page


def test_paginated_returns_empty_without_playwright(monkeypatch):
    monkeypatch.delitem(sys.modules, "playwright.sync_api", raising=False)
    monkeypatch.setitem(sys.modules, "playwright", None)
    assert render.fetch_rendered_paginated("https://x/", [], next_selector=".n",
                                           ready_selector="tbody tr") == []
