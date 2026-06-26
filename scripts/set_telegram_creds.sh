#!/usr/bin/env bash
# Securely capture the Telegram bot token + chat id into pc-insurance-digest's .env.
#
# - reads the token SILENTLY (never echoed, never stored in shell history/argv)
# - writes via a 0600 temp file, then atomically replaces .env (kept 0600)
# - upserts the three keys idempotently (re-run any time; existing values replaced)
# - optional live test confirms both creds end-to-end
#
# Usage:  bash scripts/set_telegram_creds.sh
set -euo pipefail

PROJECT_PATH="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_PATH/.env"

# ── gather input ─────────────────────────────────────────────────────────
# Silent read so the token never lands on screen, in `history`, or in `ps`.
printf 'Telegram bot token (from @BotFather, input hidden): '
read -rs TOKEN
printf '\n'
printf 'Telegram chat id (your user id from @userinfobot): '
read -r CHAT_ID

# Trim whitespace; tolerate a pasted '.../bot<TOKEN>' prefix (mirrors config.py).
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
TOKEN="${TOKEN#bot}"; TOKEN="${TOKEN#Bot}"; TOKEN="${TOKEN#BOT}"
CHAT_ID="$(printf '%s' "$CHAT_ID" | tr -d '[:space:]')"

# ── validate ─────────────────────────────────────────────────────────────
if [[ -z "$TOKEN" || -z "$CHAT_ID" ]]; then
    echo "✗ Both token and chat id are required." >&2
    exit 1
fi
if ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    echo "✗ Token doesn't look right (expected '<digits>:<letters/digits/-_>')." >&2
    exit 1
fi
if ! [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]]; then
    echo "✗ Chat id should be an integer (negative is allowed for groups)." >&2
    exit 1
fi

# ── write .env (idempotent upsert, 0600 throughout) ──────────────────────
umask 077                       # any file we create is owner-only
[[ -f "$ENV_FILE" ]] || : > "$ENV_FILE"
chmod 600 "$ENV_FILE"

TMP="$(mktemp "${TMPDIR:-/tmp}/pcenv.XXXXXX")"
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

upsert_kv() {
    # Drop any existing line for KEY, then append KEY=VALUE. Comments are kept.
    grep -v -E "^${1}=" "$ENV_FILE" > "$TMP" 2>/dev/null || true
    printf '%s=%s\n' "$1" "$2" >> "$TMP"
    cp "$TMP" "$ENV_FILE"
}

upsert_kv TELEGRAM_BOT_TOKEN "$TOKEN"
upsert_kv TELEGRAM_CHAT_ID   "$CHAT_ID"
upsert_kv NOTIFY_ENABLED     "true"
chmod 600 "$ENV_FILE"

echo "✓ Wrote TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NOTIFY_ENABLED=true to $ENV_FILE (mode 600)"

# ── optional live test ───────────────────────────────────────────────────
printf 'Send a test message now to confirm both creds? [y/N]: '
read -r DO_TEST
if [[ "$DO_TEST" =~ ^[Yy]$ ]]; then
    if ! command -v curl >/dev/null 2>&1; then
        echo "⚠ curl not found — skip; verify later with: uv run digest notify --test" >&2
        exit 0
    fi
    # Note: Telegram puts the token in the URL path (its only auth scheme), so it
    # is briefly visible to `ps` during this single call — fine on a personal host.
    resp="$(curl -s --max-time 15 \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=✅ pc-insurance-digest: Telegram credentials configured." \
        "https://api.telegram.org/bot${TOKEN}/sendMessage")"
    if printf '%s' "$resp" | grep -q '"ok":true'; then
        echo "✓ Test message sent — check your phone."
    else
        echo "✗ Telegram rejected the request. Response:" >&2
        printf '%s\n' "$resp" | sed -E "s/${TOKEN}/<token>/g" >&2
        exit 1
    fi
else
    echo "  Skipped. Verify later with: uv run digest notify --test"
fi
