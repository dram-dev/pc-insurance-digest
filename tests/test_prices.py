"""Alpha engine Phase 1 — price store round-trip + outcomes store-first read."""
from __future__ import annotations

import pytest

from digest import db, outcomes, prices


def _rows(ticker, kind, pairs, source="yahoo"):
    return [{"ticker": ticker, "date": d, "close": c, "kind": kind,
             "source": source, "fetched_at": "2026-06-09 00:00:00"}
            for d, c in pairs]


def test_upsert_and_read_back(fresh_db):
    n = db.upsert_prices(_rows("PGR", "insurer",
                               [("2026-06-01", 100.0), ("2026-06-02", 102.0)]))
    assert n == 2
    assert db.price_closes("PGR") == {"2026-06-01": 100.0, "2026-06-02": 102.0}
    assert db.latest_price_date("PGR") == "2026-06-02"
    assert db.latest_price_date("ALL") is None
    assert db.priced_tickers() == ["PGR"]


def test_upsert_is_idempotent_on_ticker_date(fresh_db):
    db.upsert_prices(_rows("IAK", "benchmark", [("2026-06-01", 50.0)]))
    db.upsert_prices(_rows("IAK", "benchmark", [("2026-06-01", 51.5)]))  # same key
    assert db.price_closes("IAK") == {"2026-06-01": 51.5}


def test_run_prices_tail_only_writes_new_days(fresh_db, monkeypatch):
    monkeypatch.setattr(prices, "_INTER_TICKER_SLEEP", 0.0)   # no politeness delay in tests
    # First a backfill via the live fetch...
    monkeypatch.setattr(prices, "fetch_daily_closes",
                        lambda t: {"2026-06-01": 10.0, "2026-06-02": 11.0})
    res = prices.run_prices(tickers=["PGR"])
    # 1 insurer × 2 days + 2 benchmarks × 2 days = 6 rows
    assert res["rows"] == 6 and res["tickers"] == 3

    # ...then a "next day" fetch: only days >= the stored max are written (the
    # boundary day is re-upserted on purpose — adjusted closes get revised).
    monkeypatch.setattr(prices, "fetch_daily_closes",
                        lambda t: {"2026-06-01": 10.0, "2026-06-02": 11.0,
                                   "2026-06-03": 12.0})
    res2 = prices.run_prices(tickers=["PGR"])
    assert res2["rows"] == 6  # 2 tail days (06-02 boundary + 06-03) × 3 tickers
    assert db.latest_price_date("PGR") == "2026-06-03"


def test_run_prices_records_skipped_on_empty_fetch(fresh_db, monkeypatch):
    monkeypatch.setattr(prices, "_INTER_TICKER_SLEEP", 0.0)
    monkeypatch.setattr(prices, "fetch_daily_closes", lambda t: {})
    res = prices.run_prices(tickers=["PGR"])
    assert set(res["skipped"]) == {"PGR", "IAK", "SPY"} and res["rows"] == 0


def test_outcomes_closes_for_prefers_store(fresh_db, monkeypatch):
    db.upsert_prices(_rows("PGR", "insurer", [("2026-06-01", 100.0)]))
    # Live fetch would return something different — store must win.
    monkeypatch.setattr(outcomes, "fetch_daily_closes",
                        lambda t: {"2026-06-01": 999.0})
    assert outcomes._closes_for("PGR") == {"2026-06-01": 100.0}


def test_outcomes_closes_for_falls_back_to_fetch(fresh_db, monkeypatch):
    monkeypatch.setattr(outcomes, "fetch_daily_closes",
                        lambda t: {"2026-06-01": 42.0})
    assert outcomes._closes_for("ALL") == {"2026-06-01": 42.0}


# ── fetch hardening: Tiingo source, precedence, 429 circuit breaker ──────────

class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} error")
    def json(self):
        return self._payload


def _reset_yahoo(monkeypatch):
    monkeypatch.setattr(outcomes, "_yahoo_session", None)
    monkeypatch.setattr(outcomes, "_yahoo_crumb", None)
    monkeypatch.setattr(outcomes, "_yahoo_blocked", False)
    monkeypatch.setattr(outcomes.time, "sleep", lambda *_: None)


def test_fetch_tiingo_parses_adjclose(monkeypatch):
    from digest.config import settings
    monkeypatch.setattr(settings, "tiingo_api_token", "tok")
    payload = [{"date": "2026-06-01T00:00:00.000Z", "close": 100.0, "adjClose": 99.5}]
    monkeypatch.setattr(outcomes.requests, "get", lambda *a, **k: _Resp(payload=payload))
    assert outcomes._fetch_tiingo("PGR") == {"2026-06-01": 99.5}


def test_fetch_tiingo_noop_without_token(monkeypatch):
    from digest.config import settings
    monkeypatch.setattr(settings, "tiingo_api_token", "")
    assert outcomes._fetch_tiingo("PGR") == {}


def test_fetch_daily_closes_prefers_tiingo(monkeypatch):
    monkeypatch.setattr(outcomes, "_fetch_tiingo", lambda t: {"2026-06-01": 5.0})
    monkeypatch.setattr(outcomes, "_fetch_yahoo", lambda t: {"2026-06-01": 9.0})
    monkeypatch.setattr(outcomes, "_fetch_stooq", lambda t: {"2026-06-01": 7.0})
    assert outcomes.fetch_daily_closes("PGR") == {"2026-06-01": 5.0}


def test_fetch_daily_closes_falls_through_to_stooq(monkeypatch):
    monkeypatch.setattr(outcomes, "_fetch_tiingo", lambda t: {})
    monkeypatch.setattr(outcomes, "_fetch_yahoo", lambda t: {})
    monkeypatch.setattr(outcomes, "_fetch_stooq", lambda t: {"2026-06-01": 7.0})
    assert outcomes.fetch_daily_closes("PGR") == {"2026-06-01": 7.0}


def test_yahoo_429_opens_circuit_breaker(monkeypatch):
    _reset_yahoo(monkeypatch)
    monkeypatch.setattr(outcomes, "_get_yahoo_session",
                        lambda: (_FakeSession(), None))
    import requests
    with pytest.raises(requests.HTTPError):
        outcomes._fetch_yahoo("PGR")           # exhausts backoff on both hosts
    assert outcomes._yahoo_blocked is True
    # next ticker short-circuits to {} without any network call
    assert outcomes._fetch_yahoo("ALL") == {}


class _FakeSession:
    def get(self, url, **k):
        return _Resp(status=429, text="Too Many Requests")
