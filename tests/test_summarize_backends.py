"""Tests for the lifted summarizer backends in digest_core.summarize.backends.

Each backend is pure transport: it takes (system_prompt, user_prompt, config),
threads the injected system prompt into the request, and returns raw text.
Network/subprocess are mocked.
"""
from __future__ import annotations

import pytest

from digest_core.summarize import backends as be

CFG = be.BackendConfig(
    timeout_sec=5,
    claude_model="sonnet",
    anthropic_api_key="k-ant",
    gemini_api_key="k-gem",
    ollama_host="http://localhost:11434",
    ollama_model="qwen2.5:14b",
    mlx_server_url="http://localhost:8080",
    mlx_model="qwen3.5",
)


def test_registry_has_all_backends():
    assert set(be.BACKENDS) == {
        "claude_cli_pro", "haiku_api", "gemini_flash_free", "local_qwen", "mlx_local",
    }


def test_api_backends_require_keys():
    no_key = be.BackendConfig()
    with pytest.raises(be.BackendError, match="anthropic_api_key"):
        be.call_haiku_api("sys", "usr", no_key)
    with pytest.raises(be.BackendError, match="gemini_api_key"):
        be.call_gemini_flash("sys", "usr", no_key)


def test_local_qwen_threads_system_prompt(monkeypatch):
    captured = {}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "qwen output"}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _R()

    monkeypatch.setattr(be.requests, "post", fake_post)
    out = be.call_local_qwen("SYSTEM-PROMPT", "the item", CFG)
    assert out == "qwen output"
    assert captured["url"].endswith("/api/generate")
    assert captured["json"]["system"] == "SYSTEM-PROMPT"   # injected, not hardcoded
    assert captured["json"]["model"] == "qwen2.5:14b"


def test_mlx_maps_choices_and_wraps_connection_error(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "mlx output"}}]}

    monkeypatch.setattr(be.requests, "post", lambda *a, **k: _R())
    assert be.call_mlx_local("sys", "usr", CFG) == "mlx output"

    def boom(*a, **k):
        raise be.requests.ConnectionError("refused")

    monkeypatch.setattr(be.requests, "post", boom)
    with pytest.raises(be.BackendError, match="not reachable"):
        be.call_mlx_local("sys", "usr", CFG)


def test_claude_cli_parses_json_envelope(monkeypatch):
    class _Result:
        returncode = 0
        stdout = '{"result": "cli output"}'
        stderr = ""

    monkeypatch.setattr(be.subprocess, "run", lambda *a, **k: _Result())
    assert be.call_claude_cli("sys", "usr", CFG) == "cli output"


def test_pc_backend_config_builds_from_settings():
    # PC's summarize re-exports BackendError and builds a config from settings.
    from digest import summarize
    assert summarize.BackendError is be.BackendError
    cfg = summarize._backend_config()
    assert isinstance(cfg, be.BackendConfig)
    assert cfg.timeout_sec == summarize.settings.summarizer_timeout_sec
    assert cfg.mlx_model == summarize.settings.mlx_model
