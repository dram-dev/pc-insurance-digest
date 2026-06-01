"""Plug-and-play LLM wiring — triage routes through the backend registry, the
BackendConfig.temperature knob flows, and `digest models` reports each stage.

Network-free: backends/probes are stubbed.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from digest import cli, triage
from digest_core.summarize import backends as be


_ITEM = {
    "id": 1, "title": "California approves 12% homeowners rate increase",
    "content": "The CDI approved a filing.", "source": "rss",
    "author": "Insurance Journal", "url": "https://x.test/1",
    "published_at": "2026-05-31", "metadata_json": "{}",
}


# ── triage routes through the pluggable registry ─────────────────────────────

def test_triage_uses_configured_backend(monkeypatch):
    captured = {}

    def fake_backend(system_prompt, user_prompt, cfg):
        captured["system"] = system_prompt
        captured["cfg"] = cfg
        return '{"decision":"keep","score":0.8,"topic":"personal_lines",' \
               '"confidence":"high","reason":"rate action"}'

    monkeypatch.setattr(triage.settings, "triage_backend", "local_qwen")
    monkeypatch.setitem(triage.BACKENDS, "local_qwen", fake_backend)

    verdict = triage.triage_item(_ITEM)
    assert verdict["decision"] == "keep"
    assert verdict["topic"] == "personal_lines"
    # triage stays deterministic + uses its own token budget, via BackendConfig
    assert captured["cfg"].temperature == 0.1
    assert captured["cfg"].max_tokens == triage.settings.triage_max_tokens
    assert captured["system"] == triage.SYSTEM_PROMPT


def test_triage_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr(triage.settings, "triage_backend", "does_not_exist")
    with pytest.raises(be.BackendError):
        triage._triage_call("prompt")


# ── BackendConfig.temperature flows into the backends ────────────────────────

def test_temperature_flows_into_local_qwen(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"response": "{}"}

    monkeypatch.setattr(be.requests, "post", lambda url, json=None, timeout=None: (sent.update(json=json) or _Resp()))
    be.call_local_qwen("sys", "usr", be.BackendConfig(ollama_model="m", temperature=0.1, max_tokens=42))
    assert sent["json"]["options"]["temperature"] == 0.1
    assert sent["json"]["options"]["num_predict"] == 42


def test_temperature_default_is_unchanged():
    assert be.BackendConfig().temperature == 0.2   # macro/other callers unaffected


# ── `digest models` reports every stage ──────────────────────────────────────

def test_models_command_lists_stages(monkeypatch):
    from digest import health
    monkeypatch.setattr(health, "check_ollama", lambda: {
        "status": "ok", "details": {"models_available": ["qwen2.5:14b", "nomic-embed-text"]}})
    monkeypatch.setattr(health, "check_mlx", lambda: {
        "status": "ok", "details": {"models": ["mlx-community/Qwen3.5-27B-4bit"]}})
    monkeypatch.setattr(health, "check_claude_cli", lambda: {"status": "ok", "details": {}})

    res = CliRunner().invoke(cli.main, ["models"])
    assert res.exit_code == 0
    for stage in ("triage", "summarize", "regime", "embeddings", "weekly"):
        assert stage in res.output
    assert "present" in res.output            # at least one reachable model
