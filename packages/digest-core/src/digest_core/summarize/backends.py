"""Summarizer backends — pure transport, no domain content.

Each backend takes ``(system_prompt, user_prompt, config)`` and returns the raw
model output string (the caller parses/normalizes it). The system prompt and
all endpoint config are injected, so these stay domain-agnostic: PC and macro
pass their own prompt + a `BackendConfig` built from their settings.

Backends:
  - claude_cli_pro:    headless `claude -p` (Claude Code subscription)
  - haiku_api:         Anthropic Messages API + prompt caching
  - gemini_flash_free: Google AI Studio free tier
  - local_qwen:        Ollama generate endpoint
  - mlx_local:         MLX-LM OpenAI-compatible server (Apple Silicon)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
from dataclasses import dataclass

import requests

_MLX_LOCK_PATH = os.environ.get("MLX_LOCK_PATH", "/tmp/digest-mlx.lock")


@contextlib.contextmanager
def _mlx_serialize():
    """Serialize MLX-server requests across processes.

    pc-insurance-digest and macro-ai-digest share one mlx_lm.server; two
    concurrent requests get batched with mismatched shapes and can Metal-OOM the
    shared server. A cross-process flock makes the two digests take turns —
    uncontended acquire is instant, contention just waits for the in-flight
    request. Degrades to a no-op if the lock can't be taken (perms / non-POSIX),
    so it can never block a summary.
    """
    fd = None
    try:
        fd = os.open(_MLX_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class BackendError(Exception):
    """Raised when a backend call fails for any reason."""


@dataclass
class BackendConfig:
    """Endpoint/model config a domain injects into the backends."""

    timeout_sec: int = 120
    max_tokens: int = 800                               # output cap (API/MLX/Ollama)
    temperature: float = 0.2                            # sampling temp (lower = more deterministic)
    claude_model: str = ""                              # claude_cli_pro --model
    anthropic_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5-20251001"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = ""
    # Ollama `think` field — None omits it entirely (required for models with
    # no thinking support, e.g. qwen2.5, where sending it is a 400); False
    # suppresses reasoning on thinking-default models (qwen3.x) so format=json
    # output stays clean and fast.
    ollama_think: bool | None = None
    mlx_server_url: str = "http://localhost:8080"
    mlx_model: str = ""


def call_claude_cli(system_prompt: str, user_prompt: str, cfg: BackendConfig) -> str:
    """Headless Claude Code via `claude -p`.

    Streams the prompt via stdin to avoid shell arg-length limits and uses
    `--output-format json` for a stable envelope.
    """
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    cmd = ["claude", "-p", "--model", cfg.claude_model, "--output-format", "json"]
    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_sec,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackendError(
            "`claude` CLI not on PATH. Install Claude Code or switch the backend."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"claude CLI timeout after {cfg.timeout_sec}s") from exc

    if result.returncode != 0:
        raise BackendError(
            f"claude CLI exit {result.returncode}: {result.stderr.strip()[:500]}"
        )

    # `claude -p --output-format json` returns an envelope with a "result" field.
    try:
        envelope = json.loads(result.stdout)
        text = envelope.get("result") or envelope.get("response") or result.stdout
    except json.JSONDecodeError:
        text = result.stdout
    return text


def call_haiku_api(system_prompt: str, user_prompt: str, cfg: BackendConfig) -> str:
    """Direct Anthropic API. Caches the system prompt."""
    if not cfg.anthropic_api_key:
        raise BackendError("anthropic_api_key not set; cannot use haiku_api backend")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": cfg.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": cfg.haiku_model,
            "max_tokens": cfg.max_tokens,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=cfg.timeout_sec,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def call_gemini_flash(system_prompt: str, user_prompt: str, cfg: BackendConfig) -> str:
    if not cfg.gemini_api_key:
        raise BackendError("gemini_api_key not set; cannot use gemini_flash_free backend")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{cfg.gemini_model}:generateContent"
    )
    r = requests.post(
        url,
        headers={"x-goog-api-key": cfg.gemini_api_key},
        json={
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": cfg.temperature,
                "responseMimeType": "application/json",
            },
        },
        timeout=cfg.timeout_sec,
    )
    r.raise_for_status()
    data = r.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def call_local_qwen(system_prompt: str, user_prompt: str, cfg: BackendConfig) -> str:
    """Ollama generate endpoint (same instance triage uses)."""
    url = cfg.ollama_host.rstrip("/") + "/api/generate"
    payload: dict = {
        "model": cfg.ollama_model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": cfg.temperature, "num_predict": cfg.max_tokens, "num_ctx": 8192},
    }
    if cfg.ollama_think is not None:
        payload["think"] = cfg.ollama_think
    r = requests.post(url, json=payload, timeout=cfg.timeout_sec)
    r.raise_for_status()
    return r.json().get("response", "")


def call_mlx_local(system_prompt: str, user_prompt: str, cfg: BackendConfig) -> str:
    """MLX-LM server (Apple Silicon), OpenAI-compatible endpoint.

    Start the server first, e.g.:
        mlx_lm.server --model <model> --port 8080

    Thinking mode is disabled via chat_template_kwargs so the model returns
    clean JSON without <think>...</think> blocks.
    """
    url = cfg.mlx_server_url.rstrip("/") + "/v1/chat/completions"
    try:
        # Serialize the generate call so PC + macro never hit the shared server
        # concurrently (held only for this request, not the whole batch).
        with _mlx_serialize():
            r = requests.post(
                url,
                json={
                    "model": cfg.mlx_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": cfg.max_tokens,
                    "temperature": cfg.temperature,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=cfg.timeout_sec,
            )
        r.raise_for_status()
    except requests.ConnectionError as exc:
        raise BackendError(
            f"MLX server not reachable at {cfg.mlx_server_url}. "
            "Start it with: mlx_lm.server --model <model> --port 8080"
        ) from exc
    except requests.Timeout as exc:
        raise BackendError(
            f"MLX server timed out after {cfg.timeout_sec}s at {cfg.mlx_server_url}. "
            "Server may have crashed — check logs and restart it."
        ) from exc
    choices = r.json().get("choices", [])
    if not choices:
        raise BackendError(f"MLX server returned no choices: {r.text[:300]}")
    return choices[0].get("message", {}).get("content", "")


BACKENDS = {
    "claude_cli_pro":     call_claude_cli,
    "haiku_api":          call_haiku_api,
    "gemini_flash_free":  call_gemini_flash,
    "local_qwen":         call_local_qwen,
    "mlx_local":          call_mlx_local,
}


def register_backend(name: str, fn) -> None:
    """Add (or override) a backend so a domain can plug in a new transport
    without editing core. `fn` must take ``(system_prompt, user_prompt, cfg)``
    and return the raw model-output string.
    """
    BACKENDS[name] = fn


def get_backend(name: str):
    """Look up a backend callable by name, or None if unregistered."""
    return BACKENDS.get(name)
