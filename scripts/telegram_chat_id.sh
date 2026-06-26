#!/usr/bin/env bash
# Discover the chat id(s) that have messaged your bot.
# RUN THIS *AFTER* you open the bot in Telegram and send it any message (/start).
# Reads the token from .env; never prints it.
#
# Usage:  bash scripts/telegram_chat_id.sh
set -euo pipefail

PROJECT_PATH="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_PATH/.env"

TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
if [[ -z "$TOKEN" ]]; then
    echo "✗ TELEGRAM_BOT_TOKEN not found in $ENV_FILE — run scripts/set_telegram_creds.sh first." >&2
    exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "✗ curl not found." >&2; exit 1; }

resp="$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getUpdates")"

printf '%s' "$resp" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if not data.get("ok"):
    print("✗ Telegram error:", data.get("description", data)); sys.exit(1)
results = data.get("result", [])
if not results:
    print("No messages yet. Open your bot in Telegram, send it any message (or tap Start),")
    print("then re-run this script.")
    sys.exit(0)
seen = {}
for u in results:
    msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid is None:
        continue
    who = (chat.get("username")
           or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
           or chat.get("title") or "?")
    seen[cid] = who
print("Chats that have messaged your bot:")
for cid, who in seen.items():
    print(f"  chat_id = {cid}   ({who})")
print()
print("Set TELEGRAM_CHAT_ID to the id for your own account — re-run scripts/set_telegram_creds.sh")
print("if it differs from what you entered.")
'
