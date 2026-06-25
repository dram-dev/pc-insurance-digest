"""App health check — reports status of all subsystems.

Run via `digest health` CLI or invoke the /health slash command in Claude Code.
Each check returns {status: "ok"|"warn"|"fail", details: {...}}.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digest import db
from digest.config import settings

LAUNCHD_LABELS = [
    "com.dr.pcdigest.am",
    "com.dr.pcdigest.pm",
    "com.dr.pcdigest.weekly",
    # com.dr.mlx.server lives with macro-ai-digest (shared MLX backend);
    # reported as a dependency so a missing server surfaces in P&C health too.
    "com.dr.mlx.server",
]


def _http_get(url: str, timeout: float = 3.0, max_bytes: int = 2000) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return -1, str(e)


def check_db() -> dict:
    try:
        with db.get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            kept  = conn.execute(
                "SELECT COUNT(*) FROM items WHERE triage_decision='keep'"
            ).fetchone()[0]
            last_row = conn.execute(
                "SELECT run_at, source, status FROM run_log ORDER BY run_at DESC LIMIT 1"
            ).fetchone()
            errors_24h = conn.execute(
                "SELECT COUNT(*) FROM run_log "
                "WHERE status='error' AND run_at >= datetime('now','-24 hours')"
            ).fetchone()[0]
        db_path = Path(settings.db_path).resolve()
        size_mb = round(db_path.stat().st_size / 1_048_576, 1)
        details: dict[str, Any] = {
            "items_total": total,
            "items_kept":  kept,
            "size_mb":     size_mb,
            "errors_24h":  errors_24h,
        }
        if last_row:
            details["last_run"] = f"{last_row['run_at'][:16]} ({last_row['source']} → {last_row['status']})"
        status = "warn" if errors_24h > 0 else "ok"
        return {"status": status, "details": details}
    except Exception as exc:
        return {"status": "fail", "details": {"error": str(exc)}}


def check_ollama() -> dict:
    url = f"{settings.ollama_host}/api/tags"
    code, body = _http_get(url)
    if code != 200:
        return {"status": "fail", "details": {"url": url, "http": code, "error": body[:100]}}
    models: list[str] = []
    try:
        data = json.loads(body)
        models = [m.get("name", "?") for m in data.get("models", [])]
    except json.JSONDecodeError:
        pass
    configured_loaded = any(settings.ollama_model in m for m in models)
    status = "warn" if (models and not configured_loaded) else "ok"
    return {
        "status": status,
        "details": {
            "url":              url,
            "models_available": models,
            "configured_model": settings.ollama_model,
            "model_loaded":     configured_loaded,
        },
    }


def check_mlx() -> dict:
    url = f"{settings.mlx_server_url}/v1/models"
    code, body = _http_get(url)
    if code != 200:
        return {"status": "fail", "details": {"url": url, "http": code, "error": body[:100]}}
    models: list[str] = []
    try:
        models = [m.get("id", "?") for m in json.loads(body).get("data", [])]
    except json.JSONDecodeError:
        pass
    return {
        "status": "ok",
        "details": {"url": url, "models": models, "configured": settings.mlx_model},
    }


def check_claude_cli() -> dict:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode == 0:
            return {"status": "ok", "details": {"version": result.stdout.strip()[:60]}}
        return {"status": "fail", "details": {"error": result.stderr.strip()[:100]}}
    except FileNotFoundError:
        return {"status": "fail", "details": {"error": "claude not found in PATH"}}
    except Exception as exc:
        return {"status": "fail", "details": {"error": str(exc)}}


def check_env() -> dict:
    required = {
        "OBSIDIAN_VAULT_PATH": bool(settings.obsidian_vault_path),
    }
    recommended = {
        "FRED_API_KEY":      bool(settings.fred_api_key),
        "REDDIT_CLIENT_ID":  bool(settings.reddit_client_id),
        "EDGAR_USER_AGENT":  bool(settings.edgar_user_agent),
    }
    optional = {
        "ANTHROPIC_API_KEY": bool(settings.anthropic_api_key),
    }
    missing_required    = [k for k, v in required.items() if not v]
    missing_recommended = [k for k, v in recommended.items() if not v]
    missing_optional    = [k for k, v in optional.items() if not v]
    status = "fail" if missing_required else ("warn" if missing_recommended else "ok")
    return {
        "status": status,
        "details": {
            "summarizer":           f"{settings.summarizer_backend} / {settings.summarizer_model}",
            "ollama_model":         settings.ollama_model,
            "missing_required":     missing_required,
            "missing_recommended":  missing_recommended,
            "missing_optional":     missing_optional,
        },
    }


def check_vault() -> dict:
    if not settings.obsidian_vault_path:
        return {"status": "fail", "details": {"error": "OBSIDIAN_VAULT_PATH not set"}}
    vault = Path(settings.obsidian_vault_path).expanduser()
    if not vault.exists():
        return {"status": "fail", "details": {"error": f"path not found: {vault}"}}
    digest_root = vault / settings.obsidian_digest_dir
    return {
        "status": "ok",
        "details": {
            "vault":        str(vault),
            "digest_dir":   settings.obsidian_digest_dir,
            "digest_exists": digest_root.exists(),
        },
    }


def check_launchd() -> dict:
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5, check=False,
        )
        lines = result.stdout.splitlines()
        jobs: dict[str, dict] = {}
        for label in LAUNCHD_LABELS:
            match = next((l for l in lines if label in l), None)
            if match:
                parts = match.split()
                jobs[label] = {
                    "loaded":    True,
                    "pid":       parts[0] if parts else "-",
                    "last_exit": parts[1] if len(parts) > 1 else "?",
                }
            else:
                jobs[label] = {"loaded": False}
        # A job with a live pid is running NOW, so a nonzero last_exit on it is a
        # previous instance that KeepAlive already restarted (recovered) — warn,
        # don't fail. A nonzero exit on a NOT-running job is a genuinely failed run
        # (e.g. a scheduled pipeline that exited non-zero), which stays a hard fail.
        def _running(j: dict) -> bool:
            return str(j.get("pid", "-")).isdigit()

        bad_exit = [
            lbl for lbl, j in jobs.items()
            if j.get("loaded") and not _running(j)
            and j.get("last_exit", "0") not in ("0", "-")
        ]
        recovered = [
            lbl for lbl, j in jobs.items()
            if j.get("loaded") and _running(j)
            and j.get("last_exit", "0") not in ("0", "-")
        ]
        not_loaded = [lbl for lbl, j in jobs.items() if not j.get("loaded")]
        status = "fail" if bad_exit else ("warn" if (recovered or not_loaded) else "ok")
        return {
            "status": status,
            "details": {"jobs": jobs, "bad_exit": bad_exit, "recovered": recovered},
        }
    except Exception as exc:
        return {"status": "fail", "details": {"error": str(exc)}}


def run_health() -> dict[str, dict]:
    """Run all health checks. Returns a dict of component → {status, details}."""
    return {
        "db":         check_db(),
        "ollama":     check_ollama(),
        "mlx":        check_mlx(),
        "claude_cli": check_claude_cli(),
        "env":        check_env(),
        "vault":      check_vault(),
        "launchd":    check_launchd(),
    }


def overall_status(report: dict[str, dict]) -> str:
    statuses = [v["status"] for v in report.values()]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"
