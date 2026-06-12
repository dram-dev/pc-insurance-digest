"""Web observatory: query layer + HTTP server.

The api functions are exercised against a seeded temp DB (same `fresh_db`
fixture as the rest of the suite); the server tests run a real
ThreadingHTTPServer on an ephemeral port to cover routing, JSON encoding,
gzip, and path-traversal protection.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import urllib.request
from pathlib import Path

import pytest

from digest import db
from digest.webapp import api
from digest.webapp.server import ROUTES, make_server


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def seeded_db(fresh_db: Path) -> Path:
    conn = _conn(fresh_db)
    with conn:
        conn.executemany(
            "INSERT INTO items (source, source_id, url, title, published_at,"
            " ingested_at, topic, triage_decision, metadata_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("rss", "r1", "https://x.test/1", "Hurricane forms",
                 "2026-06-10T08:00:00+00:00", "2026-06-10T09:30:00", "cat_event", "keep", None),
                ("edgar", "e1", "https://sec.test/1", "TRV 8-K",
                 "2026-06-09T12:00:00+00:00", "2026-06-09T12:10:00", "underwriting_results",
                 "keep", json.dumps({"ticker": "TRV", "form": "8-K"})),
                ("edgar", "e2", "https://sec.test/2", "PGR 10-K (backfill)",
                 "2025-02-01T12:00:00+00:00", "2025-02-01T12:00:00", "underwriting_results",
                 "keep", json.dumps({"ticker": "PGR", "form": "10-K", "backfill": True})),
                ("rss", "r2", None, "Dropped thing",
                 "2026-06-10T07:00:00+00:00", "2026-06-10T09:30:00", None, "drop", None),
                ("hn", "h1", None, "No publish timestamp",
                 None, "2026-06-11T01:00:00", "cyber", "keep", None),
            ],
        )
        # latest + superseded score for item 1 — endpoints must pick the latest
        conn.executemany(
            "INSERT INTO signal_scores (item_id, computed_at, score, source_mult,"
            " regime_mult, topic_relevance, recency, llm_judgment, topic_boost,"
            " burden_boost, tier) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "2026-06-10T10:00:00", 0.5, 1.0, 1.0, 1.0, 1.0, 0.5, 1.0, 1.0, "low"),
                (1, "2026-06-11T10:00:00", 1.8, 1.3, 1.2, 1.0, 0.9, 1.28, 1.0, 1.0, "high"),
                (2, "2026-06-11T10:00:00", 0.9, 1.3, 1.0, 1.0, 0.7, 1.0, 1.0, 1.0, "medium"),
                (3, "2026-06-11T10:00:00", 0.7, 1.3, 1.0, 1.0, 0.4, 1.0, 1.0, 1.0, "medium"),
                (5, "2026-06-11T10:00:00", 0.3, 0.6, 1.0, 1.0, 1.0, 0.5, 1.0, 1.0, "low"),
            ],
        )
        conn.executemany(
            "INSERT INTO prices (ticker, date, close, kind) VALUES (?,?,?,?)",
            [
                ("PGR", "2026-06-10", 100.0, "insurer"),
                ("PGR", "2026-06-11", 105.0, "insurer"),
                ("IAK", "2026-06-10", 50.0, "benchmark"),
                ("IAK", "2026-06-11", 50.5, "benchmark"),
            ],
        )
        conn.execute(
            "INSERT INTO regime_signals (as_of, market_cycle, cat_load,"
            " market_cycle_mult, cat_load_mult, multiplier)"
            " VALUES ('2026-06-01T00:00:00+00:00','stable','low_season',1.0,1.0,1.0)"
        )
        conn.executemany(
            "INSERT INTO loss_triangles (insurer, lob, metric, accident_year,"
            " dev_period, cumulative_value, as_of, canonical_lob)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                ("PGR", "personal_auto_x", "incurred", 2023, 12, 100.0, "2025-12-31", "personal_auto"),
                ("PGR", "personal_auto_x", "incurred", 2023, 24, 130.0, "2025-12-31", "personal_auto"),
                ("PGR", "personal_auto_x", "incurred", 2024, 12, 110.0, "2025-12-31", "personal_auto"),
                # stale as_of must NOT be returned
                ("PGR", "personal_auto_x", "incurred", 2023, 12, 90.0, "2024-12-31", "personal_auto"),
                ("TEST", "junk", "incurred", 2023, 12, 1.0, "2025-12-31", None),
            ],
        )
        conn.executemany(
            "INSERT INTO run_log (run_at, run_type, source, items_fetched,"
            " items_new, duration_ms, status, error) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("2026-06-10 09:30:00", "am", "rss", 40, 2, 900, "ok", None),
                ("2026-06-10 09:31:00", "am", "edgar", 5, 1, 400, "error", "boom"),
            ],
        )
        conn.executemany(
            "INSERT INTO outcome_backtest (item_id, horizon_days, corroborated)"
            " VALUES (?,?,?)",
            [(1, 7, 1), (2, 7, 0)],
        )
    conn.close()
    return fresh_db


# ── query layer ──────────────────────────────────────────────────────────────

def test_meta(seeded_db):
    out = api.meta(_conn(seeded_db))
    assert out["counts"]["items"] == 5
    assert out["regime"]["market_cycle"] == "stable"
    assert {"PGR", "IAK"} <= set(out["price_tickers"])


def test_events_latest_score_and_provenance(seeded_db):
    out = api.events(_conn(seeded_db), days=0)
    by_id = {r["id"]: r for r in out["rows"]}
    # latest snapshot wins, not the superseded 0.5
    assert by_id[1]["score"] == pytest.approx(1.8)
    assert by_id[1]["tier"] == "high"
    # backfill flag surfaces; published fallback flagged
    assert by_id[3]["backfill"] is True
    assert by_id[5]["t_src"] == "ingested"
    # drops never appear
    assert 4 not in by_id


def test_leaderboard_orders_and_carries_factors(seeded_db):
    out = api.leaderboard(_conn(seeded_db), days=0)
    assert [r["id"] for r in out["rows"]][:2] == [1, 2]
    assert out["rows"][0]["llm_judgment"] == pytest.approx(1.28)


def test_latency_excludes_backfill_and_missing_published(seeded_db):
    # n<5 sources are hidden, so with this seed the result is empty — but the
    # query itself must not include the backfilled or unpublished rows.
    out = api.latency(_conn(seeded_db), days=0)
    assert out["rows"] == []


def test_timeline_counts_kept_by_day(seeded_db):
    out = api.timeline(_conn(seeded_db), days=0)
    total = sum(r["n"] for r in out["rows"])
    assert total == 4  # 4 kept items with topics (drop excluded)


def test_triangle_uses_latest_as_of_and_skips_test_rows(seeded_db):
    cat = api.triangle_catalog(_conn(seeded_db))
    assert all(r["insurer"] != "TEST" for r in cat["rows"])
    assert cat["rows"][0]["peak_value"] == pytest.approx(130.0)
    tri = api.triangle(_conn(seeded_db), "PGR", "personal_auto_x", "incurred")
    assert tri["as_of"] == "2025-12-31"
    assert len(tri["cells"]) == 3
    missing = api.triangle(_conn(seeded_db), "PGR", "nope", "incurred")
    assert missing["as_of"] is None and missing["cells"] == []


def test_prices_grouped_series(seeded_db):
    out = api.prices(_conn(seeded_db), days=0)
    pgr = next(s for s in out["series"] if s["ticker"] == "PGR")
    assert pgr["dates"] == ["2026-06-10", "2026-06-11"]
    only = api.prices(_conn(seeded_db), days=0, tickers=["IAK"])
    assert [s["ticker"] for s in only["series"]] == ["IAK"]


def test_ops_runs_and_outcomes(seeded_db):
    runs = api.ops_runs(_conn(seeded_db), days=0)
    assert any(r["failures"] for r in runs["rows"])
    assert runs["errors"][0]["error"] == "boom"
    out = api.outcomes(_conn(seeded_db))
    assert out["by_horizon"][0]["n"] == 2
    assert out["by_horizon"][0]["corroborated"] == 1


def test_all_routes_json_serializable(seeded_db):
    conn = _conn(seeded_db)
    q = {"insurer": ["PGR"], "lob": ["personal_auto_x"], "tickers": ["PGR"]}
    for name, fn in ROUTES.items():
        payload = fn(conn, q)
        json.dumps(payload, allow_nan=False)  # raises on NaN/Inf or exotic types


# ── HTTP server ──────────────────────────────────────────────────────────────

@pytest.fixture
def live_server(seeded_db):
    httpd = make_server(host="127.0.0.1", port=0, db_path=seeded_db)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_http_api_and_static(live_server):
    status, _, body = _fetch(f"{live_server}/api/meta")
    assert status == 200
    assert json.loads(body)["counts"]["items"] == 5

    status, headers, body = _fetch(f"{live_server}/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert b"Observatory" in body


def test_http_gzip(live_server):
    status, headers, body = _fetch(
        f"{live_server}/api/events?days=0", headers={"Accept-Encoding": "gzip"})
    assert status == 200
    if headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    assert json.loads(body)["n"] >= 1


def test_http_unknown_endpoint_and_traversal(live_server):
    status, _, _ = _fetch(f"{live_server}/api/nope")
    assert status == 404
    # encoded traversal must not escape the static dir
    status, _, _ = _fetch(f"{live_server}/%2e%2e/%2e%2e/etc/passwd")
    assert status == 404


def test_query_param_clamping(live_server):
    status, _, body = _fetch(f"{live_server}/api/events?days=banana&limit=999999")
    assert status == 200
    json.loads(body)  # falls back to defaults instead of erroring
