"""Run-quality gate on `digest pipeline`.

Required stages (ingest → triage → summarize → publish) must make the run exit
non-zero on failure so launchd/cron can't mistake a broken run for a good one;
enrichment stages (regime, signals, price store) stay best-effort.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from digest import cli, obsidian, prices, regime, signals, summarize, triage


class _FakeRegime:
    def summary_line(self) -> str:
        return "stable × low_season"


def _boom(msg: str):
    """Return a stage stub that raises — for failure-path tests."""
    def _fn(*args, **kwargs):
        raise RuntimeError(msg)
    return _fn


@pytest.fixture
def stub_stages(monkeypatch):
    """Patch every pipeline stage to a benign success; tests override one at a time."""
    monkeypatch.setattr(cli.db, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(cli, "run_ingest", lambda *a, **k: (0, 0))
    monkeypatch.setattr(triage, "run_triage",
                        lambda *a, **k: {"kept": 1, "dropped": 0, "errors": 0})
    monkeypatch.setattr(summarize, "run_summarize",
                        lambda *a, **k: {"succeeded": 1, "failed": 0, "ready": 1})
    monkeypatch.setattr(regime, "is_stale", lambda *a, **k: False)
    monkeypatch.setattr(regime, "current_regime", lambda *a, **k: _FakeRegime())
    monkeypatch.setattr(regime, "compute_regime", lambda *a, **k: _FakeRegime())
    monkeypatch.setattr(signals, "run_signals", lambda *a, **k: {"scored": 1})
    monkeypatch.setattr(prices, "run_prices", lambda *a, **k: {"rows": 0, "skipped": []})
    monkeypatch.setattr(obsidian, "publish",
                        lambda *a, **k: {"daily_items": 1, "topic_archives": 1, "daily_path": "x"})
    return monkeypatch


def test_pipeline_all_ok_exits_zero(stub_stages):
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "manual"])
    assert res.exit_code == 0, res.output
    assert "run quality" in res.output
    assert "all stages ok" in res.output


def test_pipeline_publish_failure_exits_nonzero(stub_stages):
    stub_stages.setattr(obsidian, "publish", _boom("vault locked"))
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "manual"])
    assert res.exit_code == 1, res.output
    assert "publish failed" in res.output
    assert "publish (required): vault locked" in res.output


def test_pipeline_optional_failure_still_exits_zero(stub_stages):
    stub_stages.setattr(signals, "run_signals", _boom("no scores"))
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "manual"])
    assert res.exit_code == 0, res.output
    assert "signals (optional): no scores" in res.output
    # publish still ran → the run completes despite the optional miss
    assert "stage 6: publish" in res.output


def test_pipeline_required_upstream_failure_skips_publish_and_exits_nonzero(stub_stages):
    stub_stages.setattr(summarize, "run_summarize", _boom("mlx down"))
    res = CliRunner().invoke(cli.main, ["pipeline", "--run-type", "manual"])
    assert res.exit_code == 1, res.output
    assert "required stage failed: mlx down" in res.output
    assert "upstream failure" in res.output  # publish skipped, not attempted


def test_pipeline_skip_publish_exits_zero(stub_stages):
    res = CliRunner().invoke(cli.main, ["pipeline", "--skip-publish"])
    assert res.exit_code == 0, res.output
    assert "all stages ok" in res.output


def test_optional_failure_with_markup_chars_stays_non_fatal(stub_stages):
    # An exception message carrying Rich-markup tokens must not be re-parsed as
    # markup: "[/]" would raise MarkupError (crashing a best-effort stage) and
    # "[active]" would be silently swallowed. Escaping keeps the optional failure
    # non-fatal and preserves the message verbatim.
    stub_stages.setattr(signals, "run_signals", _boom("broke [/] on [active] items"))
    res = CliRunner().invoke(cli.main, ["pipeline"])
    assert res.exit_code == 0, res.output            # optional stage stays non-fatal
    assert "signals (optional)" in res.output
    assert "[active]" in res.output                  # bracket content preserved, not eaten
