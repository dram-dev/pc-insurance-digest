"""LegiScan ingestor (Lead 9) — getSearch parse, recency filter, cap, no-op.

Network-free: stub requests.get with a synthetic getSearch payload.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from digest.ingest.legiscan import LegiScanIngestor


def _recent(days: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).date().isoformat()


class _Resp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        ...
    def json(self):
        return self._p


def test_noop_without_key(monkeypatch):
    monkeypatch.setattr("digest.ingest.legiscan.settings.legiscan_api_key", "")
    ing = LegiScanIngestor()
    assert ing.enabled is False
    assert ing.fetch() == []


def test_search_filters_stale_and_stamps_state(monkeypatch):
    monkeypatch.setattr("digest.ingest.legiscan.settings.legiscan_api_key", "testkey")
    payload = {"status": "OK", "searchresult": {
        "summary": {"count": 2},
        "0": {"relevance": 100, "bill_id": 111, "bill_number": "SB876",
              "title": "Fire and residential property insurance.",
              "last_action": "Read second time.", "last_action_date": _recent(5),
              "state": "CA", "url": "https://legiscan.com/CA/bill/SB876"},
        "1": {"relevance": 80, "bill_id": 222, "bill_number": "AB10",
              "title": "Old insurance bill.", "last_action": "Chaptered.",
              "last_action_date": "2020-01-01", "state": "CA", "url": "u2"},
    }}
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return _Resp(payload)

    monkeypatch.setattr("digest.ingest.legiscan.requests.get", fake_get)
    ing = LegiScanIngestor()
    ing.states = ["CA"]
    items = ing.fetch()

    assert captured["params"]["op"] == "getSearch"
    assert captured["params"]["state"] == "CA"
    assert len(items) == 1                       # the 2020 bill is filtered by recency
    it = items[0]
    assert it.source == "legiscan" and it.source_id == "111"
    assert it.title == "[CA SB876] Fire and residential property insurance."
    assert it.metadata["state"] == "CA"
    assert it.metadata["topic_hint"] == "regulatory_rate"
    assert it.metadata["bill_number"] == "SB876"
    assert it.published_at is not None


def test_max_per_state_cap(monkeypatch):
    monkeypatch.setattr("digest.ingest.legiscan.settings.legiscan_api_key", "testkey")
    sr = {"summary": {"count": 20}}
    for i in range(20):
        sr[str(i)] = {"relevance": 100 - i, "bill_id": 1000 + i, "bill_number": f"SB{i}",
                      "title": f"insurance bill {i}", "last_action": "x",
                      "last_action_date": _recent(3), "state": "CA", "url": "u"}
    monkeypatch.setattr("digest.ingest.legiscan.requests.get",
                        lambda *a, **k: _Resp({"status": "OK", "searchresult": sr}))
    ing = LegiScanIngestor()
    ing.states = ["CA"]
    ing.max_per_state = 5
    assert len(ing.fetch()) == 5


def test_non_ok_status_returns_empty(monkeypatch):
    monkeypatch.setattr("digest.ingest.legiscan.settings.legiscan_api_key", "testkey")
    monkeypatch.setattr("digest.ingest.legiscan.requests.get",
                        lambda *a, **k: _Resp({"status": "ERROR", "alert": {"message": "bad key"}}))
    ing = LegiScanIngestor()
    ing.states = ["CA"]
    assert ing.fetch() == []
