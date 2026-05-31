"""CourtListener v4 query params (regression).

`filed_after` is a *search*-endpoint param and 400s on /dockets/; the dockets
endpoint wants the django-filter lookup `date_filed__gte`. Network-free: stub
requests.get to capture the params the ingestor actually sends.
"""
from __future__ import annotations


def test_dockets_query_uses_date_filed_gte(monkeypatch):
    from digest.ingest import courtlistener as cl

    monkeypatch.setattr(cl.settings, "courtlistener_token", "testtoken")
    monkeypatch.setattr(cl.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_request_count", 0, raising=False)

    captured: list[dict] = []

    class _R:
        status_code = 200
        def raise_for_status(self): ...
        def json(self): return {"results": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.append(params)
        return _R()

    monkeypatch.setattr(cl.requests, "get", fake_get)

    ing = cl.CourtListenerIngestor()
    ing.config = {"tier1": ["cand"], "emerging": [], "tier3": [], "mdl_keywords": []}
    ing.fetch()

    assert captured, "ingestor issued no request"
    p = captured[0]
    assert "date_filed__gte" in p          # the django-filter lookup
    assert "filed_after" not in p          # the param that 400s on /dockets/
    assert p["order_by"] == "-date_filed"
    assert p["court"] == "cand"
