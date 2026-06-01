#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATUS_FILE="$ROOT/local-runtime/codex-status-display/status.json"
RESOLVER="$SCRIPT_DIR/touchbar_chat_window_resolver.py"
MODE="${1:-bar}"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-2}"
REFRESH_LOCK_DIR="$ROOT/local-runtime/codex-status-display/.touchbar-refresh.lock"
LOCK_STALE_SECONDS="${LOCK_STALE_SECONDS:-15}"

cleanup_stale_lock() {
  local lock_mtime now_epoch lock_age
  if [[ ! -d "$REFRESH_LOCK_DIR" ]]; then
    return
  fi
  now_epoch="$(date +%s)"
  lock_mtime="$(stat -f %m "$REFRESH_LOCK_DIR" 2>/dev/null || echo 0)"
  if [[ "$lock_mtime" -le 0 ]]; then
    return
  fi
  lock_age=$((now_epoch - lock_mtime))
  if [[ "$lock_age" -ge "$LOCK_STALE_SECONDS" ]]; then
    rmdir "$REFRESH_LOCK_DIR" >/dev/null 2>&1 || true
  fi
}

refresh_with_lock() {
  cleanup_stale_lock
  if mkdir "$REFRESH_LOCK_DIR" 2>/dev/null; then
    trap 'rmdir "$REFRESH_LOCK_DIR" >/dev/null 2>&1 || true' RETURN
    python3 "$ROOT/services/codex-status-display/codex_status_display.py" --root "$ROOT" --quiet >/dev/null 2>&1 || true
    trap - RETURN
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
  if [[ "$file_age" -ge "$MAX_AGE_SECONDS" ]]; then
    refresh_with_lock
  fi
}

resolve_slot_label() {
  local slot="$1"
  local info label valid
  info="$(python3 "$RESOLVER" resolve --status-file "$STATUS_FILE" --slot "$slot" 2>/dev/null || true)"
  valid="$(printf '%s\n' "$info" | awk -F'=' '/^valid=/{print $2; exit}')"
  valid="${valid:-0}"
  if [[ "$valid" != "1" ]]; then
    # Empty output hides the widget in BetterTouchTool.
    echo ""
    return
  fi
  label="$(printf '%s\n' "$info" | awk -F'=' '/^label=/{print $2; exit}')"
  if [[ -z "$label" ]]; then
    label="Chat${slot}"
  fi
  printf '● %s\n' "$label"
}

resolve_nav_text() {
  local side="$1"
  local info left_enabled right_enabled
  info="$(python3 "$RESOLVER" dump --status-file "$STATUS_FILE" 2>/dev/null || true)"
  left_enabled="$(printf '%s\n' "$info" | awk -F'=' '/^left_enabled=/{print $2; exit}')"
  right_enabled="$(printf '%s\n' "$info" | awk -F'=' '/^right_enabled=/{print $2; exit}')"
  left_enabled="${left_enabled:-0}"
  right_enabled="${right_enabled:-0}"
  if [[ "$side" == "left" ]]; then
    if [[ "$left_enabled" == "1" ]]; then
      echo "◀"
    else
      echo "◁"
    fi
    return
  fi
  if [[ "$right_enabled" == "1" ]]; then
    echo "▶"
  else
    echo "▷"
  fi
}

resolve_run_text() {
  python3 - "$STATUS_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    payload = {}
counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
print(f"RUN: {int(counts.get('running_projects', 0) or 0):02d}")
PY
}

render_mode() {
  python3 - "$STATUS_FILE" "$MODE" <<'PY'
import json
import re
import sqlite3
import sys
from pathlib import Path

status_path = sys.argv[1]
mode = sys.argv[2]

def short_text(value: object, limit: int = 18) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def clean_label(value: object, limit: int = 12) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[@([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"['\"]/Users/[^'\"]+['\"]", "", text)
    text = re.sub(r"^['\"]?/Users/[^'\"]+['\"]?", "", text)
    text = re.sub(r"^['\"]?/Users/\S+", "", text).strip(" -,:;|")
    text = text.splitlines()[0].strip()
    # Reject path-like / noisy labels so Touch Bar keeps a clean visual look.
    lowered = text.lower()
    if (
        not text
        or text.startswith("/")
        or "/" in text
        or text.startswith("Users ")
        or lowered.startswith("users ")
        or lowered.startswith("http")
        or lowered.startswith("file:")
    ):
        return ""
    if not text:
        text = "Chat"
    return short_text(text, limit)

payload = {}
try:
    with open(status_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    payload = {}

counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
awaiting = payload.get("awaiting") if isinstance(payload.get("awaiting"), list) else []
projects = payload.get("projects") if isinstance(payload.get("projects"), list) else []

def recent_chat_titles(payload_obj: dict, limit: int = 3) -> list[str]:
    sources = payload_obj.get("sources") if isinstance(payload_obj.get("sources"), dict) else {}
    db_path = Path(str(sources.get("codex_threads") or "")).expanduser()
    if not db_path.exists():
        return []
    query = """
        select title
        from threads
        where archived = 0
        order by coalesce(updated_at_ms, 0) desc, updated_at desc
        limit 12
    """
    out: list[str] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        with con:
            for (raw_title,) in con.execute(query):
                text = str(raw_title or "").strip()
                if not text:
                    continue
                first_line = text.splitlines()[0].strip()
                if not first_line:
                    continue
                label = clean_label(first_line.replace("codex://threads/", ""), 12)
                if label and label not in out:
                    out.append(label)
                if len(out) >= limit:
                    break
    except Exception:
        return []
    return out


def recent_active_thread_count(payload_obj: dict, minutes: int = 20) -> int:
    sources = payload_obj.get("sources") if isinstance(payload_obj.get("sources"), dict) else {}
    db_path = Path(str(sources.get("codex_threads") or "")).expanduser()
    if not db_path.exists():
        return 0
    now_ms = int(__import__("time").time() * 1000)
    cutoff = now_ms - max(1, minutes) * 60 * 1000
    query = """
        select count(*)
        from threads
        where archived = 0
          and coalesce(updated_at_ms, 0) >= ?
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        with con:
            row = con.execute(query, (cutoff,)).fetchone()
        return int((row or [0])[0] or 0)
    except Exception:
        return 0

done_count = int(counts.get("completed_today", 0) or 0)
run_count = int(counts.get("running_projects", 0) or 0)
approve_count = sum(
    1
    for item in awaiting
    if isinstance(item, dict) and str(item.get("status") or "").strip().lower() != "pending_plan"
)
if not awaiting:
    approve_count = int(counts.get("awaiting_response", 0) or 0)
if run_count <= 0:
    run_count = recent_active_thread_count(payload, minutes=20)

chat_candidates: list[str] = []
for item in awaiting:
    if isinstance(item, dict):
        title = clean_label(item.get("short_label") or item.get("title"), 12)
        if title and title not in chat_candidates:
            chat_candidates.append(title)
for item in projects:
    if isinstance(item, dict):
        name = clean_label(item.get("short_label") or item.get("name"), 12)
        if name and name not in chat_candidates:
            chat_candidates.append(name)
for item in recent_chat_titles(payload):
    if item and item not in chat_candidates:
        chat_candidates.append(item)
# Keep stable designer-like placeholders when dynamic candidates are missing.
fallback_chats = ["Chat1", "Chat2", "TESS"]
for fb in fallback_chats:
    if len(chat_candidates) >= 3:
        break
    chat_candidates.append(fb)
while len(chat_candidates) < 3:
    chat_candidates.append(f"Chat{len(chat_candidates)+1}")
chat_title = chat_candidates[0]

if mode == "done":
    print(f"DONE: {done_count:02d}")
elif mode == "run":
    print(f"RUN: {run_count:02d}")
elif mode == "chat":
    print(f"CHAT {chat_title}")
elif mode == "chat1":
    print(f"● {chat_candidates[0]}")
elif mode == "chat2":
    print(f"● {chat_candidates[1]}")
elif mode == "chat3":
    print(f"● {chat_candidates[2]}")
elif mode in {"approve", "approve_label"}:
    print("Approve ?" if approve_count <= 0 else f"Approve {approve_count}?")
else:
    print(f"DONE: {done_count:02d}  RUN: {run_count:02d}  CHAT {chat_title}")
PY
}

refresh_status_if_needed
if [[ "$MODE" =~ ^chat(10|[1-9])$ ]]; then
  resolve_slot_label "${MODE#chat}"
  exit 0
fi

case "$MODE" in
  run)
    resolve_run_text
    ;;
  chat1)
    resolve_slot_label 1
    ;;
  chat2)
    resolve_slot_label 2
    ;;
  chat3)
    resolve_slot_label 3
    ;;
  nav_left)
    resolve_nav_text left
    ;;
  nav_right)
    resolve_nav_text right
    ;;
  *)
    render_mode
    ;;
esac
