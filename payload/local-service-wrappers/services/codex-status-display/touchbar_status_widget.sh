#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATUS_FILE="$ROOT/local-runtime/codex-status-display/status.json"
MODE="${1:-bar}"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-30}"
REFRESH_LOCK_DIR="$ROOT/local-runtime/codex-status-display/.touchbar-refresh.lock"

refresh_with_lock() {
  if mkdir "$REFRESH_LOCK_DIR" 2>/dev/null; then
    python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true
    rmdir "$REFRESH_LOCK_DIR" >/dev/null 2>&1 || true
  fi
}

refresh_status_if_needed() {
  local now_mtime now_epoch file_age
  now_epoch="$(date +%s)"
  if [[ ! -f "$STATUS_FILE" ]]; then
    refresh_with_lock
    return
  fi

  now_mtime="$(stat -f %m "$STATUS_FILE" 2>/dev/null || echo 0)"
  if [[ "$now_mtime" -le 0 ]]; then
    refresh_with_lock
    return
  fi
  file_age=$((now_epoch - now_mtime))
  if [[ "$file_age" -gt "$MAX_AGE_SECONDS" ]]; then
    refresh_with_lock
  fi
}

render_mode() {
  python3 - "$STATUS_FILE" "$MODE" <<'PY'
import json
import sys

status_path = sys.argv[1]
mode = sys.argv[2]

def short_text(value: object, limit: int = 18) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

payload = {}
try:
    with open(status_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    payload = {}

counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
awaiting = payload.get("awaiting") if isinstance(payload.get("awaiting"), list) else []
projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []

done_count = int(counts.get("completed_today", 0) or 0)
run_count = int(counts.get("running_projects", 0) or 0)
approve_count = int(counts.get("awaiting_response", 0) or 0)

chat_title = ""
if awaiting and isinstance(awaiting[0], dict):
    chat_title = short_text(awaiting[0].get("title"), 16)
elif projects and isinstance(projects[0], dict):
    chat_title = short_text(projects[0].get("name"), 16)
chat_title = chat_title or "No active"

if mode == "done":
    print(f"DONE {done_count}")
elif mode == "run":
    print(f"RUN {run_count}")
elif mode == "chat":
    print(f"* {chat_title}")
elif mode in {"approve", "approve_label"}:
    print("Approve ?" if approve_count <= 0 else f"Approve {approve_count}")
else:
    print(f"DONE {done_count}  RUN {run_count}  * {chat_title}")
PY
}

refresh_status_if_needed
render_mode
