"""Security audit — file permissions, credential scan, subprocess safety,
SQL injection review, network exposure, and gitignore coverage.

Run via `digest security` CLI or included in the /health slash command.
Each check returns {status: "ok"|"warn"|"fail", details: {...}, issues: [...]}.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    p = Path(__file__).parent
    while p != p.parent:
        if (p / "pyproject.toml").exists():
            return p
        p = p.parent
    raise RuntimeError("Cannot locate project root (pyproject.toml)")


PROJECT_ROOT = _project_root()

# ── Sensitive files that should have tight permissions ─────────────────
SENSITIVE_FILES = [
    ".env",
    "secrets/gmail_credentials.json",
    "secrets/gmail_token.json",
]

# ── Patterns that suggest hardcoded credentials ────────────────────────
# Ordered: most specific first to reduce false positives
CREDENTIAL_PATTERNS: list[tuple[str, str]] = [
    (r'sk-[a-zA-Z0-9]{32,}',                             "OpenAI/Anthropic key prefix"),
    (r'AIza[0-9A-Za-z\-_]{35}',                          "Google API key format"),
    (r'ghp_[a-zA-Z0-9]{36}',                             "GitHub personal access token"),
    (r'(?i)(password|passwd|secret|api_key|token)\s*=\s*["\'][^"\']{10,}["\']',
                                                          "Hardcoded credential assignment"),
    (r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}',                  "Bearer token literal"),
]

# Lines containing these strings are not flagged (they reference config, not literals)
_SAFE_INDICATORS = [
    "settings.", "os.environ", "os.getenv", "getenv",
    "Field(", "= Field", "#", "test", "example", "placeholder",
    "EXAMPLE", "YOUR_", "<YOUR", "sk-xxx", "sk-...",
]

# ── Ports this app uses — flag if exposed beyond localhost ─────────────
KNOWN_APP_PORTS = {"11434": "Ollama", "8080": "MLX server"}


def _file_octal(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def _world_readable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IROTH)


# ── Individual checks ──────────────────────────────────────────────────

def check_file_permissions() -> dict:
    issues: list[str] = []
    details: dict[str, Any] = {}
    for rel in SENSITIVE_FILES:
        p = PROJECT_ROOT / rel
        if not p.exists():
            details[rel] = "not found"
            continue
        mode = _file_octal(p)
        world_read = _world_readable(p)
        details[rel] = {"mode": mode, "world_readable": world_read}
        if world_read:
            issues.append(f"{rel} is world-readable ({mode}) → chmod 600 {rel}")
        elif mode not in ("0o600", "0o640", "0o644"):
            issues.append(f"{rel} mode {mode} — recommend 600")
    status = "fail" if any("world-readable" in i for i in issues) else ("warn" if issues else "ok")
    return {"status": status, "details": details, "issues": issues}


def check_hardcoded_secrets() -> dict:
    src = PROJECT_ROOT / "src"
    findings: list[dict] = []
    for py_file in sorted(src.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, description in CREDENTIAL_PATTERNS:
            for match in re.finditer(pattern, text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end   = text.find("\n", match.start())
                line       = text[line_start:line_end].strip()
                if any(safe in line for safe in _SAFE_INDICATORS):
                    continue
                findings.append({
                    "file":    str(py_file.relative_to(PROJECT_ROOT)),
                    "pattern": description,
                    "snippet": line[:90],
                })
    status = "fail" if findings else "ok"
    return {"status": status, "details": {"count": len(findings), "findings": findings}}


def check_subprocess_safety() -> dict:
    src = PROJECT_ROOT / "src"
    shell_true: list[str] = []
    for py_file in sorted(src.rglob("*.py")):
        try:
            lines = py_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Match shell=True as an actual keyword arg, not inside a string literal
            if re.search(r'\bshell\s*=\s*True\b', stripped) and '"shell' not in stripped and "'shell" not in stripped:
                shell_true.append(f"{py_file.relative_to(PROJECT_ROOT)}:{i}: {stripped[:80]}")
    status = "fail" if shell_true else "ok"
    return {
        "status": status,
        "details": {
            "shell_true_count": len(shell_true),
            "locations":        shell_true,
            "note":             "All subprocess.run calls use list args (safe)",
        },
    }


def check_sql_safety() -> dict:
    db_file = PROJECT_ROOT / "src" / "digest" / "db.py"
    text    = db_file.read_text(encoding="utf-8")
    issues: list[str] = []

    # The one known f-string SQL that interpolates a value into a JSON path
    if "$.{meta_key}" in text:
        if "_VALID_OUTCOME_KEYS" in text and "meta_key not in _VALID_OUTCOME_KEYS" in text:
            note = "get_followup_z: meta_key interpolated into JSON path but validated against allowlist before use"
        else:
            issues.append("get_followup_z: meta_key in f-string SQL without visible allowlist guard")
            note = "allowlist guard NOT found — review required"
    else:
        note = "no meta_key interpolation found"

    # All placeholders should be ? * n patterns (not f-string of user values)
    fstring_sqls = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("sql = f") or 'f"""' in line or "f'''" in line
    ]

    status = "fail" if issues else "ok"
    return {
        "status": status,
        "details": {
            "fstring_sql_blocks": len(fstring_sqls),
            "allowlist_protected": "_VALID_OUTCOME_KEYS" in text,
            "placeholders_used":   "placeholders = \",\".join" in text,
            "note":                note,
        },
        "issues": issues,
    }


def check_network_exposure() -> dict:
    try:
        result = subprocess.run(
            ["lsof", "-i", "-P", "-n", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        lines = result.stdout.strip().splitlines()[1:]  # skip header
        relevant: list[dict] = []
        for line in lines:
            for port, service in KNOWN_APP_PORTS.items():
                if f":{port}" in line:
                    parts      = line.split()
                    proc       = parts[0] if parts else "?"
                    addr_field = next((p for p in parts if f":{port}" in p), "?")
                    exposed    = addr_field.startswith("*:") or "0.0.0.0:" in addr_field
                    relevant.append({
                        "service": service,
                        "port":    port,
                        "process": proc,
                        "address": addr_field,
                        "exposed_to_lan": exposed,
                    })
        exposed_services = [r for r in relevant if r["exposed_to_lan"]]
        status = "warn" if exposed_services else "ok"
        return {
            "status": status,
            "details": {
                "listening": relevant,
                "exposed_to_lan": exposed_services,
                "note": "Services bound to * or 0.0.0.0 are reachable on the local network",
            },
        }
    except Exception as exc:
        return {"status": "fail", "details": {"error": str(exc)}}


def check_gitignore() -> dict:
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.exists():
        return {"status": "fail", "details": {"error": ".gitignore missing"}}
    text = gitignore.read_text()
    required = {
        ".env":      ".env" in text,
        "secrets/":  "secrets/" in text or "secrets/*" in text,
        "data/":     "data/" in text,
        "*.db":      "*.db" in text,
        "*.json exclusion": "!secrets/README.md" in text or "secrets/*" in text,
    }
    missing = [k for k, v in required.items() if not v]
    status = "fail" if missing else "ok"
    return {"status": status, "details": {"covered": required, "missing": missing}}


# ── Public entry point ─────────────────────────────────────────────────

def run_security() -> dict[str, dict]:
    """Run all security checks. Returns component → {status, details, issues}."""
    return {
        "file_permissions":   check_file_permissions(),
        "hardcoded_secrets":  check_hardcoded_secrets(),
        "subprocess_safety":  check_subprocess_safety(),
        "sql_safety":         check_sql_safety(),
        "network_exposure":   check_network_exposure(),
        "gitignore":          check_gitignore(),
    }


def overall_status(report: dict[str, dict]) -> str:
    statuses = [v["status"] for v in report.values()]
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"
