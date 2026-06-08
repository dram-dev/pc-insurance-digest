"""DatabricksSink resilience — a failed/unreachable warehouse must never hang or
raise; it latches off for the run so SQLite stays the source of truth.
"""
from __future__ import annotations

import sys
import types

from digest_core.sinks.databricks import DatabricksSink

_CELL = {"insurer": "x", "lob": "l", "metric": "incurred", "accident_year": 2024,
         "dev_period": 12, "cumulative_value": 1.0, "as_of": "2024-12-31"}


def _sink() -> DatabricksSink:
    return DatabricksSink(enabled=True, host="h", http_path="p", token="t", catalog="c")


def test_disabled_sink_is_a_noop():
    s = DatabricksSink(enabled=False, host="", http_path="", token="", catalog="")
    s.write_triangle_cells([_CELL])          # must not connect or raise
    s.write_xbrl_facts([{"fact_key": "k", "insurer": "x", "dataset": "d",
                         "concept": "c", "value": 1.0}])
    assert s._broken is False


def test_connection_failure_latches_off(monkeypatch):
    """A connect that fails (unreachable host / bad creds) latches _broken so a
    180-row write triggers ONE failed attempt, not 180 hangs — and never raises."""
    s = _sink()
    calls = {"connect": 0}

    fake_sql = types.ModuleType("databricks.sql")

    def boom(**kwargs):
        calls["connect"] += 1
        raise OSError("warehouse unreachable")

    fake_sql.connect = boom
    fake_pkg = types.ModuleType("databricks")
    fake_pkg.sql = fake_sql
    monkeypatch.setitem(sys.modules, "databricks", fake_pkg)
    monkeypatch.setitem(sys.modules, "databricks.sql", fake_sql)

    # First write attempts to connect, fails, latches off — no exception escapes.
    s.write_triangle_cells([_CELL])
    assert s._broken is True
    assert calls["connect"] == 1

    # Subsequent writes short-circuit on the latch — no further connect attempts.
    s.write_triangle_cells([_CELL] * 50)
    s.write_statutory_facts([{"fact_key": "k", "insurer": "i", "source": "iii",
                              "dataset": "premiums", "value": 1.0}])
    assert calls["connect"] == 1
