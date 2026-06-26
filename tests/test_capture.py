"""Forward-to-capture — URL detection, tweet/article/text routing, file output.

Never touches the network: fetch_tweet / fetch_fulltext are monkeypatched. The
clip dir is pointed at a tmp path so writes are hermetic.
"""
from __future__ import annotations

import pytest
import yaml

from digest import capture
from digest.config import settings


@pytest.fixture
def clip_dir(tmp_path, monkeypatch):
    """Point OBSIDIAN_CLIP_DIR at an absolute tmp dir (used as-is)."""
    monkeypatch.setattr(settings, "obsidian_clip_dir", str(tmp_path))
    return tmp_path


def test_first_url():
    assert capture.first_url("see https://a.test/x?q=1 ok") == "https://a.test/x?q=1"
    assert capture.first_url("no link here") is None
    assert capture.first_url(None) is None


def _read_clip(path):
    text = path.read_text(encoding="utf-8")
    _, fm, body = text.split("---", 2)
    return yaml.safe_load(fm), body.strip()


def test_capture_text_only(clip_dir):
    res = capture.capture("A bare note about reserve releases")
    assert res["kind"] == "text"
    assert res["chars"] == len("A bare note about reserve releases")
    fm, body = _read_clip(res["path"])
    assert body == "A bare note about reserve releases"
    assert "telegram" in fm["tags"]
    assert fm["source"] == "telegram:capture"


def test_capture_resolves_tweet(clip_dir, monkeypatch):
    monkeypatch.setattr(
        capture, "fetch_tweet",
        lambda url: {"text": "Cat losses are mounting", "author": "@analyst"},
    )
    res = capture.capture("https://x.com/analyst/status/123")
    assert res["kind"] == "tweet"
    fm, body = _read_clip(res["path"])
    assert body == "Cat losses are mounting"
    assert fm["author"] == "@analyst"
    assert fm["source"] == "https://x.com/analyst/status/123"


def test_capture_falls_back_to_article(clip_dir, monkeypatch):
    monkeypatch.setattr(capture, "fetch_tweet", lambda url: None)
    monkeypatch.setattr(capture, "fetch_fulltext", lambda url: "Full article body text.")
    res = capture.capture("https://example.com/news/reinsurance")
    assert res["kind"] == "article"
    _, body = _read_clip(res["path"])
    assert body == "Full article body text."


def test_capture_raises_on_empty(clip_dir):
    with pytest.raises(ValueError):
        capture.capture("")
