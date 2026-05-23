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

for label in am pm weekly; do
    src="$PROJECT_PATH/launchd/com.dr.pcdigest.${label}.plist"
    dst="$LAUNCH_AGENTS/com.dr.pcdigest.${label}.plist"

    if [[ ! -f "$src" ]]; then
        echo "✗ Missing template: $src"
        exit 1
    fi

    sed -e "s|__PROJECT_PATH__|$PROJECT_PATH|g" \
        -e "s|__UV_PATH__|$UV_PATH|g" \
        "$src" > "$dst"

    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
    echo "✓ Loaded $label job"
done

echo ""
echo "Verify with: launchctl list | grep com.dr.pcdigest"
echo "Logs will appear in: $PROJECT_PATH/logs/"
