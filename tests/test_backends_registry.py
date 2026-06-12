"""Tests for the backend registry + BackendConfig (the LLM-transport seam)."""
from __future__ import annotations

from digest_core.summarize import backends
from digest_core.summarize.backends import BackendConfig, get_backend, register_backend


def test_builtin_backends_present():
    for name in ("claude_cli_pro", "haiku_api", "gemini_flash_free",
                 "local_qwen", "mlx_local"):
        assert get_backend(name) is not None


def test_register_backend_roundtrip():
    def _fake(system_prompt, user_prompt, cfg):
        return f"{system_prompt}|{user_prompt}|{cfg.max_tokens}"

    try:
        register_backend("_fake_test_backend", _fake)
        fn = get_backend("_fake_test_backend")
        assert fn is _fake
        assert fn("sys", "usr", BackendConfig(max_tokens=42)) == "sys|usr|42"
        # registered in the shared dict too
        assert "_fake_test_backend" in backends.BACKENDS
    finally:
        # don't leak into other tests that assert the exact backend set
        backends.BACKENDS.pop("_fake_test_backend", None)


def test_get_backend_unknown_returns_none():
    assert get_backend("_nope_not_a_backend") is None


def test_backend_config_max_tokens_default_is_800():
    assert BackendConfig().max_tokens == 800
    assert BackendConfig(max_tokens=600).max_tokens == 600


def test_local_qwen_think_field_omitted_unless_set(monkeypatch):
    """Ollama 400s when `think` is sent to a non-thinking model (qwen2.5), so
    the key must be absent by default — and present (false) for qwen3.x where
    reasoning would pollute the format=json output."""
    captured: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "{}"}

    def _fake_post(url, json=None, timeout=None):
        captured.append(json)
        return _Resp()

    monkeypatch.setattr(backends.requests, "post", _fake_post)

    backends.call_local_qwen("s", "u", BackendConfig())          # default: None
    assert "think" not in captured[0]

    backends.call_local_qwen("s", "u", BackendConfig(ollama_think=False))
    assert captured[1]["think"] is False
