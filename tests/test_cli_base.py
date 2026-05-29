"""Tests for the generic CLI ingest mechanics in digest_core.cli.base."""
from __future__ import annotations

from digest_core.cli import base


class _FakeConsole:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def rule(self, msg):
        self.lines.append(("rule", str(msg)))

    def print(self, msg):
        self.lines.append(("print", str(msg)))


def test_load_ingestor_resolves_class():
    cls = base.load_ingestor("digest.ingest.rss:RSSIngestor")
    assert cls.__name__ == "RSSIngestor"


def test_run_ingest_tallies_and_isolates_failures(monkeypatch):
    class _Good:
        def run(self, run_type="manual"):
            return (5, 2)

    class _Bad:
        def run(self, run_type="manual"):
            raise RuntimeError("boom")

    fakes = {"x:Good": _Good, "x:Bad": _Bad}
    monkeypatch.setattr(base, "load_ingestor", lambda d: fakes[d])

    console = _FakeConsole()
    mapping = {"good": "x:Good", "bad": "x:Bad", "good2": "x:Good"}
    fetched, new = base.run_ingest(mapping, ["good", "bad", "good2"], "manual", console)

    assert (fetched, new) == (10, 4)   # the failing source contributes 0
    assert any("bad" in m and "✗" in m for kind, m in console.lines if kind == "print")


def test_run_ingest_per_source_rule_toggle(monkeypatch):
    class _Good:
        def run(self, run_type="manual"):
            return (1, 1)

    monkeypatch.setattr(base, "load_ingestor", lambda d: _Good)
    console = _FakeConsole()
    base.run_ingest({"a": "x:G"}, ["a"], "manual", console, per_source_rule=False)
    assert not any(kind == "rule" for kind, _ in console.lines)
