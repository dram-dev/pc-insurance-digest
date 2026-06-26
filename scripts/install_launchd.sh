#!/usr/bin/env bash
# Install launchd jobs for pc-insurance-digest on the Mac mini.
# Run from the project root: bash scripts/install_launchd.sh

set -euo pipefail

PROJECT_PATH="$(cd "$(dirname "$0")/.." && pwd)"
UV_PATH="$(command -v uv || true)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

if [[ -z "$UV_PATH" ]]; then
    echo "✗ 'uv' not found in PATH. Install uv first: brew install uv"
    exit 1
fi

echo "Project: $PROJECT_PATH"
echo "uv:      $UV_PATH"
echo "Target:  $LAUNCH_AGENTS"

mkdir -p "$LAUNCH_AGENTS"
mkdir -p "$PROJECT_PATH/logs"

for label in am pm weekly learn askbot; do
    src="$PROJECT_PATH/launchd/com.dr.pcdigest.${label}.plist"
    dst="$LAUNCH_AGENTS/com.dr.pcdigest.${label}.plist"

    if [[ ! -f "$src" ]]; then
        echo "✗ Missing template: $src"
        exit 1
    fi

    sed -e "s|__PROJECT_PATH__|$PROJECT_PATH|g" \
        -e "s|__UV_PATH__|$UV_PATH|g" \
        "$src" > "$dst"

    # Use the modern bootstrap/bootout API. The legacy `launchctl load` is
    # deprecated and silently no-ops on macOS 11+ (confirmed on macOS 26 /
    # Darwin 25), so a plain `load` leaves the job un-registered.
    launchctl bootout "gui/$(id -u)/com.dr.pcdigest.${label}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$dst"
    echo "✓ Loaded $label job"
done

echo ""
echo "Verify with: launchctl list | grep com.dr.pcdigest"
echo "Logs will appear in: $PROJECT_PATH/logs/"
