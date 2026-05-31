"""parse_date — ISO-8601 (with tz offset / Z) plus the strptime format union."""
from __future__ import annotations

from digest.parse.dates import parse_date


def test_iso_offset_normalized_to_utc():
    dt = parse_date("2026-05-28T13:42:01-04:00")
    assert dt is not None
    assert dt.isoformat() == "2026-05-28T17:42:01+00:00"   # -04:00 → UTC


def test_iso_z_suffix():
    dt = parse_date("2026-05-28T13:42:01Z")
    assert dt is not None and dt.hour == 13 and dt.tzinfo is not None


def test_plain_iso_date_and_named_month():
    assert parse_date("2026-05-28").year == 2026
    assert parse_date("May 28, 2026").month == 5


def test_none_and_unparseable():
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("not a date") is None
