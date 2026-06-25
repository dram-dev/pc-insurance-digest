"""check_launchd status logic.

A KeepAlive service (e.g. the shared MLX server) that crashed and was auto-
restarted shows a nonzero `last_exit` while running — that's recovered, a
warning, not a whole-system fail. A nonzero exit on a job that is NOT running
is a genuinely failed scheduled run and stays a hard fail.
"""
from __future__ import annotations

from types import SimpleNamespace

from digest import health


def _fake_launchctl(rows, monkeypatch):
    """rows: {label: (pid, last_exit)} → patch `launchctl list` to that output."""
    out = "\n".join(f"{pid}\t{exitc}\t{label}" for label, (pid, exitc) in rows.items())
    monkeypatch.setattr(health.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))


def _all_idle_ok():
    return {lbl: ("-", "0") for lbl in health.LAUNCHD_LABELS}


def test_all_jobs_idle_and_clean_is_ok(monkeypatch):
    _fake_launchctl(_all_idle_ok(), monkeypatch)
    res = health.check_launchd()
    assert res["status"] == "ok"
    assert res["details"]["bad_exit"] == []


def test_running_service_with_stale_crash_exit_is_warn_not_fail(monkeypatch):
    rows = _all_idle_ok()
    target = next((lbl for lbl in health.LAUNCHD_LABELS if "mlx" in lbl), list(rows)[0])
    rows[target] = ("78660", "-6")            # up now (live pid) but the last instance SIGABRT'd
    _fake_launchctl(rows, monkeypatch)
    res = health.check_launchd()
    assert res["status"] == "warn"            # recovered — not the whole system failing
    assert target in res["details"]["recovered"]
    assert res["details"]["bad_exit"] == []


def test_not_running_job_with_nonzero_exit_is_fail(monkeypatch):
    rows = _all_idle_ok()
    target = list(rows)[0]
    rows[target] = ("-", "1")                 # a scheduled job that actually failed its last run
    _fake_launchctl(rows, monkeypatch)
    res = health.check_launchd()
    assert res["status"] == "fail"
    assert target in res["details"]["bad_exit"]
