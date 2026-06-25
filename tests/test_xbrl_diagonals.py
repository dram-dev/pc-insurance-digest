"""Second-diagonal backfill: n-th-10-K selection + the diagonal walk in run_ingest.

These cover the only new logic for lighting up reserve_deterioration_boost across
the XBRL universe — selecting a *prior* annual 10-K (so a second `as_of` snapshot
lands) and looping the requested number of diagonals best-effort. The extraction
math is unchanged and exercised by test_xbrl_facts.py.
"""
from __future__ import annotations

from digest import edgar_triangle_extract as ext
from digest import edgar_xbrl_ingest as ing


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


# Two 10-Qs / an 8-K interleaved with three annual 10-Ks, newest-first — the real
# shape of a filer's `recent` window.
_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "10-Q", "10-K", "10-K"],
            "accessionNumber": ["0000-00", "0001-25", "0000-0q", "0002-24", "0003-23"],
            "primaryDocument": [
                "x.htm", "trv-20251231.htm", "q.htm",
                "trv-20241231.htm", "trv-20231231.htm",
            ],
            "filingDate": ["2026-03-01", "2026-02-12", "2025-11-01",
                           "2025-02-13", "2024-02-15"],
            "reportDate": ["2026-02-28", "2025-12-31", "2025-09-30",
                           "2024-12-31", "2023-12-31"],
        }
    }
}


def test_nth_10k_walks_annual_diagonals(monkeypatch):
    monkeypatch.setattr(ext, "_get", lambda url, timeout=60: _FakeResp(_SUBMISSIONS))

    # n=0 = latest annual (FY2025); 8-K/10-Q are skipped.
    assert ext._nth_10k("0000086312", 0) == (
        "000125", "trv-20251231.htm", "2026-02-12", "2025-12-31")
    # n=1 = the prior-year 10-K — the second diagonal we backfill.
    assert ext._nth_10k("0000086312", 1) == (
        "000224", "trv-20241231.htm", "2025-02-13", "2024-12-31")
    assert ext._nth_10k("0000086312", 2)[1] == "trv-20231231.htm"
    # Past the end of the available 10-Ks → all-None, not an IndexError.
    assert ext._nth_10k("0000086312", 9) == (None, None, None, None)
    # The latest-only wrapper stays a backward-compatible 3-tuple.
    assert ext._latest_10k("0000086312") == ("000125", "trv-20251231.htm", "2026-02-12")


def _patch_ingest(monkeypatch, *, fetch):
    """Stub the network + DB so run_ingest exercises only the diagonal walk."""
    monkeypatch.setattr(ing, "fetch_instance_xml", fetch)
    monkeypatch.setattr(
        ing, "extract_facts",
        lambda xml, insurer: [{"dataset": "triangle",
                               "as_of": "2025-12-31" if "n=0" in xml else "2024-12-31"}])
    monkeypatch.setattr(ing, "triangle_cells_from_facts", lambda facts: [1, 2, 3])
    monkeypatch.setattr(ing.db, "upsert_xbrl_facts", lambda f: None)
    monkeypatch.setattr(ing.db, "upsert_triangle_cells", lambda c: len(c))
    monkeypatch.setattr(ing, "insurer_universe", lambda: [("TRV", "0000086312")])


def test_run_ingest_default_is_single_diagonal(monkeypatch):
    seen: list[int] = []

    def fetch(cik, n=0):
        seen.append(n)
        return f"<xml n={n}>", "2026-02-12"

    _patch_ingest(monkeypatch, fetch=fetch)
    res = ing.run_ingest()  # diagonals defaults to 1 → latest only
    assert seen == [0]
    assert len(res) == 1 and res[0]["as_of"] == "2025-12-31"


def test_run_ingest_walks_two_diagonals(monkeypatch):
    seen: list[int] = []

    def fetch(cik, n=0):
        seen.append(n)
        return f"<xml n={n}>", "2026-02-12"

    _patch_ingest(monkeypatch, fetch=fetch)
    res = ing.run_ingest(diagonals=2)
    assert seen == [0, 1]  # latest then prior year
    assert {r["as_of"] for r in res} == {"2025-12-31", "2024-12-31"}
    assert all(r["triangle_cells"] == 3 for r in res)


def test_run_ingest_isolates_a_missing_prior_diagonal(monkeypatch):
    def fetch(cik, n=0):
        if n >= 1:
            raise RuntimeError(f"no 10-K at diagonal n={n}")
        return "<xml n=0>", "2026-02-12"

    _patch_ingest(monkeypatch, fetch=fetch)
    res = ing.run_ingest(diagonals=2)
    assert len(res) == 2
    assert res[0]["as_of"] == "2025-12-31"
    assert "error" in res[1] and res[1]["n"] == 1  # missing prior logged, not fatal
