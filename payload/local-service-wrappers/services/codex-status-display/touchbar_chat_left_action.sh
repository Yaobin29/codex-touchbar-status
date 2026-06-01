#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RESOLVER="$SCRIPT_DIR/touchbar_chat_window_resolver.py"
SYNC_SCRIPT="$HOME/.codex/bin/sync_touchbar_codex_env.py"

python3 "$RESOLVER" navigate --direction left >/dev/null 2>&1 || true
python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true
if [[ -x "$SYNC_SCRIPT" ]]; then
  "$SYNC_SCRIPT" >/dev/null 2>&1 || true
fi
