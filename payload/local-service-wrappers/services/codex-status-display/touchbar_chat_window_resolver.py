#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATUS_FILE = Path(
    "~/Library/Application Support/CodexTouchBar/local-runtime/codex-status-display/status.json"
).expanduser()
DEFAULT_STATE_FILE = Path(
    "~/Library/Application Support/CodexTouchBar/local-runtime/codex-status-display/chat-window-state.json"
).expanduser()

DISPLAY_CJK_LIMIT = 7
DISPLAY_WORD_LIMIT = 3
DISPLAY_ASCII_LIMIT = 22
DISPLAY_LABEL_LIMIT = DISPLAY_ASCII_LIMIT
VISIBLE_SLOT_COUNT = 10
MAX_CANDIDATES = 10
PREFIX_RE = re.compile(r"^(computer|codex|chat|thread|assistant)\b[\s:：\-_/|]*", re.IGNORECASE)
PLAN_MARKER_TOKENS = ("<proposed_plan>", "</proposed_plan>")


@dataclass
class Candidate:
    label: str
    thread_id: str
    status: str
    key: str = ""


def short_text(value: object, limit: int = 18) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def display_label_text(value: object) -> str:
    text = str(value or "").strip()
    if re.search(r"[\u4e00-\u9fff]", text):
        cjk_compact = "".join(re.findall(r"[\u4e00-\u9fff]+", text))
        if cjk_compact:
            return cjk_compact[:DISPLAY_CJK_LIMIT]
        return short_text(text, DISPLAY_ASCII_LIMIT)
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.-]*", text)
    if words:
        selected: list[str] = []
        for word in words[:DISPLAY_WORD_LIMIT]:
            candidate = " ".join([*selected, word])
            if len(candidate) <= DISPLAY_ASCII_LIMIT or not selected:
                selected.append(word)
        return " ".join(selected)
    return short_text(text, DISPLAY_ASCII_LIMIT)


def clean_label(value: object, limit: int = DISPLAY_LABEL_LIMIT) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[@([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"['\"]/Users/[^'\"]+['\"]", "", text)
    text = re.sub(r"^['\"]?/Users/[^'\"]+['\"]?", "", text)
    text = re.sub(r"^['\"]?/Users/\S+", "", text).strip(" -,:;|")
    text = text.splitlines()[0].strip()
    # Remove generic prefixes so labels carry useful information.
    text = PREFIX_RE.sub("", text).strip(" -,:;|")
    text = re.sub(r"\s+", " ", text).strip()
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
    return display_label_text(text)


def infer_item_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "awaiting_response").strip().lower() or "awaiting_response"
    if status == "pending_plan":
        return status
    marker_text = " ".join(
        str(item.get(key, "") or "").lower() for key in ("title", "short_label", "preview", "status")
    )
    if any(token in marker_text for token in PLAN_MARKER_TOKENS):
        return "pending_plan"
    return status


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return payload if isinstance(payload, type(default)) else default


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_candidates(status_payload: dict[str, Any]) -> list[Candidate]:
    awaiting = status_payload.get("awaiting") if isinstance(status_payload.get("awaiting"), list) else []
    projects = status_payload.get("projects") if isinstance(status_payload.get("projects"), list) else []
    completed = status_payload.get("completed") if isinstance(status_payload.get("completed"), list) else []

    out: list[Candidate] = []
    seen_ids: set[str] = set()

    def add_candidate(
        label_raw: object,
        thread_id_raw: object,
        status: str,
        short_label_raw: object = "",
        key_raw: object = "",
    ) -> None:
        label = clean_label(short_label_raw, DISPLAY_LABEL_LIMIT) or clean_label(label_raw, DISPLAY_LABEL_LIMIT)
        thread_id = str(thread_id_raw or "").strip()
        key = str(key_raw or "").strip()
        if not label or not thread_id:
            return
        if thread_id in seen_ids:
            return
        seen_ids.add(thread_id)
        out.append(Candidate(label=label, thread_id=thread_id, status=status, key=key))

    for item in awaiting:
        if not isinstance(item, dict):
            continue
        status = infer_item_status(item)
        add_candidate(item.get("title") or item.get("name"), item.get("id"), status, item.get("short_label"))

    for item in projects:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in {"awaiting_response", "pending_approval", "pending_plan"}:
            continue
        if status != "running":
            continue
        add_candidate(item.get("name") or item.get("title"), item.get("id"), "running", item.get("short_label"))

    for item in completed:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "done_unread").strip().lower() or "done_unread"
        # Read completions are intentionally silent; only unread completed chats stay visible.
        if status not in {"done_unread", "completed_unread"}:
            continue
        add_candidate(
            item.get("title") or item.get("name"),
            item.get("id"),
            "done_unread",
            item.get("short_label"),
            item.get("key"),
        )

    return out


def clamp_offset(raw_offset: int, total: int) -> int:
    max_offset = max(total - VISIBLE_SLOT_COUNT, 0)
    return max(0, min(raw_offset, max_offset))


def load_offset(state_file: Path) -> int:
    state = load_json(state_file, {})
    if not isinstance(state, dict):
        return 0
    raw = state.get("offset")
    if isinstance(raw, int):
        return raw
    try:
        return int(raw or 0)
    except Exception:
        return 0


def save_offset(state_file: Path, offset: int) -> None:
    payload = {
        "offset": int(offset),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(state_file, payload)


def emit_resolve(
    candidates: list[Candidate],
    offset: int,
    slot: int,
) -> None:
    total = len(candidates)
    max_offset = max(total - VISIBLE_SLOT_COUNT, 0)
    left_enabled = 1 if offset > 0 else 0
    right_enabled = 1 if offset < max_offset else 0

    visible_index = offset + slot - 1
    valid = 1 if 0 <= visible_index < total else 0
    if valid:
        item = candidates[visible_index]
        thread_id = item.thread_id
        label = item.label
        status = item.status
        key = item.key
    else:
        thread_id = ""
        label = f"Chat{slot}"
        status = "empty"
        key = ""

    print(f"slot={slot}")
    print(f"thread={thread_id}")
    print(f"label={label}")
    print(f"status={status}")
    print(f"key={key}")
    print(f"valid={valid}")
    print(f"offset={offset}")
    print(f"max_offset={max_offset}")
    print(f"total={total}")
    print(f"left_enabled={left_enabled}")
    print(f"right_enabled={right_enabled}")


def emit_dump(candidates: list[Candidate], offset: int) -> None:
    total = len(candidates)
    max_offset = max(total - VISIBLE_SLOT_COUNT, 0)
    left_enabled = 1 if offset > 0 else 0
    right_enabled = 1 if offset < max_offset else 0

    print(f"total={total}")
    print(f"offset={offset}")
    print(f"max_offset={max_offset}")
    print(f"left_enabled={left_enabled}")
    print(f"right_enabled={right_enabled}")

    for slot in range(1, VISIBLE_SLOT_COUNT + 1):
        visible_index = offset + slot - 1
        valid = 1 if 0 <= visible_index < total else 0
        if valid:
            item = candidates[visible_index]
            thread_id = item.thread_id
            label = item.label
            status = item.status
            key = item.key
        else:
            thread_id = ""
            label = f"Chat{slot}"
            status = "empty"
            key = ""
        print(f"slot{slot}_valid={valid}")
        print(f"slot{slot}_thread={thread_id}")
        print(f"slot{slot}_label={label}")
        print(f"slot{slot}_status={status}")
        print(f"slot{slot}_key={key}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve Touch Bar chat window slots with left/right paging.")
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--status-file", default=str(DEFAULT_STATUS_FILE), help="Path to status.json")
    shared.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="Path to chat-window-state.json")

    p_resolve = sub.add_parser("resolve", parents=[shared], help=f"Resolve one visible slot (1..{VISIBLE_SLOT_COUNT}).")
    p_resolve.add_argument("--slot", type=int, required=True, choices=range(1, VISIBLE_SLOT_COUNT + 1))

    p_nav = sub.add_parser("navigate", parents=[shared], help="Move offset left or right by one step.")
    p_nav.add_argument("--direction", choices=["left", "right"], required=True)

    sub.add_parser("dump", parents=[shared], help="Dump window state and visible slot mapping.")

    args = parser.parse_args()

    status_file = Path(args.status_file).expanduser()
    state_file = Path(args.state_file).expanduser()

    status_payload = load_json(status_file, {})
    if not isinstance(status_payload, dict):
        status_payload = {}
    candidates = load_candidates(status_payload)[:MAX_CANDIDATES]

    offset = clamp_offset(load_offset(state_file), len(candidates))
    if args.command == "navigate":
        if args.direction == "left":
            offset = clamp_offset(offset - 1, len(candidates))
        else:
            offset = clamp_offset(offset + 1, len(candidates))
    save_offset(state_file, offset)

    if args.command == "resolve":
        emit_resolve(candidates, offset, args.slot)
        return 0
    if args.command == "dump":
        emit_dump(candidates, offset)
        return 0
    if args.command == "navigate":
        emit_dump(candidates, offset)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
