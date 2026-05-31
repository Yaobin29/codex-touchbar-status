#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATUS_FILE="$ROOT/local-runtime/codex-status-display/status.json"

python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true

APPROVAL_INFO="$(
  python3 - "$STATUS_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
data = {}
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    data = {}

counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
awaiting = data.get("awaiting") if isinstance(data.get("awaiting"), list) else []

count = int(counts.get("awaiting_response", 0) or 0)
thread_id = ""
title = ""
if awaiting and isinstance(awaiting[0], dict):
    thread_id = str(awaiting[0].get("id") or "").strip()
    title = str(awaiting[0].get("title") or "").strip()

print(count)
print(thread_id)
print(title)
PY
)"
PENDING_COUNT="$(printf '%s\n' "$APPROVAL_INFO" | sed -n '1p')"
TARGET_THREAD="$(printf '%s\n' "$APPROVAL_INFO" | sed -n '2p')"
TARGET_TITLE="$(printf '%s\n' "$APPROVAL_INFO" | sed -n '3p')"
PENDING_COUNT="${PENDING_COUNT:-0}"
TARGET_THREAD="${TARGET_THREAD:-}"
TARGET_TITLE="${TARGET_TITLE:-}"

if [[ "$PENDING_COUNT" -le 0 ]]; then
  osascript -e 'display notification "No pending approvals right now." with title "Codex Approve"' >/dev/null 2>&1 || true
  exit 0
fi

PROMPT_TITLE="Codex Approve"
PROMPT_TEXT="Pending approvals: ${PENDING_COUNT}"
if [[ -n "$TARGET_TITLE" ]]; then
  PROMPT_TEXT="${PROMPT_TEXT}"$'\n'"Open first: ${TARGET_TITLE}"
fi

CONFIRMED="$(
  osascript - "$PROMPT_TEXT" "$PROMPT_TITLE" 2>/dev/null <<'OSA'
on run argv
  set promptText to item 1 of argv
  set promptTitle to item 2 of argv
  try
    return button returned of (display dialog promptText with title promptTitle buttons {"Cancel", "Open"} default button "Open")
  on error number -128
    return "Cancel"
  end try
end run
OSA
)" || CONFIRMED="Open"

if [[ "$CONFIRMED" != "Open" ]]; then
  exit 0
fi

if [[ -n "$TARGET_THREAD" ]]; then
  open "codex://threads/${TARGET_THREAD}" >/dev/null 2>&1 || true
  exit 0
fi

open -a "Codex" >/dev/null 2>&1 || true
