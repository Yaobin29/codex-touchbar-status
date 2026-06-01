#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATUS_FILE="$ROOT/local-runtime/codex-status-display/status.json"
RESOLVER="$SCRIPT_DIR/touchbar_chat_window_resolver.py"
SYNC_SCRIPT="$HOME/.codex/bin/sync_touchbar_codex_env.py"
INDEX="${1:-1}"

if [[ ! "$INDEX" =~ ^(10|[1-9])$ ]]; then
  INDEX="1"
fi

python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true

TARGET_INFO="$({
python3 "$RESOLVER" resolve --status-file "$STATUS_FILE" --slot "$INDEX"
} )"

TARGET_THREAD="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^thread=/{print $2; exit}')"
TARGET_LABEL="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^label=/{print $2; exit}')"
TARGET_STATUS="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^status=/{print $2; exit}')"
TARGET_KEY="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^key=/{print $2; exit}')"
TOTAL_CANDIDATES="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^total=/{print $2; exit}')"
OFFSET="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^offset=/{print $2; exit}')"
LEFT_ENABLED="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^left_enabled=/{print $2; exit}')"
RIGHT_ENABLED="$(printf '%s\n' "$TARGET_INFO" | awk -F'=' '/^right_enabled=/{print $2; exit}')"
TARGET_THREAD="${TARGET_THREAD:-}"
TARGET_LABEL="${TARGET_LABEL:-}"
TARGET_STATUS="${TARGET_STATUS:-}"
TARGET_KEY="${TARGET_KEY:-}"
TOTAL_CANDIDATES="${TOTAL_CANDIDATES:-0}"
OFFSET="${OFFSET:-0}"
LEFT_ENABLED="${LEFT_ENABLED:-0}"
RIGHT_ENABLED="${RIGHT_ENABLED:-0}"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf 'slot=%s\nthread=%s\nlabel=%s\n' "$INDEX" "$TARGET_THREAD" "$TARGET_LABEL"
  printf 'status=%s\nkey=%s\n' "$TARGET_STATUS" "$TARGET_KEY"
  printf 'offset=%s\ntotal=%s\nleft_enabled=%s\nright_enabled=%s\n' "$OFFSET" "$TOTAL_CANDIDATES" "$LEFT_ENABLED" "$RIGHT_ENABLED"
  exit 0
fi

if [[ -z "$TARGET_THREAD" ]]; then
  exit 0
fi

open "codex://threads/${TARGET_THREAD}" >/dev/null 2>&1 || open -a "Codex" >/dev/null 2>&1 || true

if [[ "$TARGET_STATUS" =~ ^(done_unread|completed_unread)$ && -n "$TARGET_KEY" ]]; then
  python3 "$ROOT/services/codex-status-display/codex_status_display.py" \
    --root "$ROOT" \
    --mark-seen-key "$TARGET_KEY" \
    --quiet >/dev/null 2>&1 || true
  python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true
  if [[ -x "$SYNC_SCRIPT" ]]; then
    "$SYNC_SCRIPT" >/dev/null 2>&1 || true
  fi
  "$HOME/.local/bin/codex-touchbar-live-sync" once >/dev/null 2>&1 || true
fi
