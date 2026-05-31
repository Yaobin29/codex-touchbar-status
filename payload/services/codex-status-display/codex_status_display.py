#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_SERIAL = "/dev/cu.usbmodem111201"
STATE_DIR_NAME = "codex-status-display"
WIRE_SCHEMA_VERSION = 1
DEFAULT_MAX_WIRE_BYTES = 1024
APP_CONFIG_NAME = "app-config.json"
APP_DIST_DIR = Path(__file__).resolve().parent / "app" / "dist"
FIRMWARE_ROOT = Path(__file__).resolve().parent / "firmware"
DEFAULT_PROJECT_WHITELIST = {
    "PaperBanana": ("projects/PaperBanana", "paperbanana"),
    "digital-twin-chip": ("projects/digital-twin-chip", "digital-twin-chip"),
}

DEVICE_PROFILES: dict[str, dict[str, Any]] = {
    "legacy": {
        "id": "legacy",
        "label": "Legacy ESP32/ST7789 135x240",
        "firmware_dir": "esp32_st7789_serial",
        "fqbn": "esp32:esp32:esp32",
        "default_port": "/dev/cu.wchusbserial11120",
        "display": "135x240 ST7789",
        "preset": "legacy_stable",
        "interval": 5,
        "max_wire_bytes": 520,
    },
    "c6": {
        "id": "c6",
        "label": "ESP32-C6/ST7789 172x320",
        "firmware_dir": "esp32c6_st7789_status",
        "fqbn": "esp32:esp32:esp32c6",
        "default_port": "/dev/cu.usbmodem111201",
        "display": "172x320 ST7789",
        "preset": "c6_dynamic",
        "interval": 1,
        "max_wire_bytes": 1024,
    },
}

DISPLAY_PRESETS: dict[str, dict[str, Any]] = {
    "legacy_stable": {
        "label": "Legacy stable",
        "interval": 5,
        "max_wire_bytes": 520,
        "toggles": {
            "run_count": True,
            "done_count": True,
            "need_response": True,
            "active_list": True,
            "manual_items": True,
        },
    },
    "c6_dynamic": {
        "label": "C6 dynamic",
        "interval": 1,
        "max_wire_bytes": 1024,
        "toggles": {
            "run_count": True,
            "done_count": True,
            "need_response": True,
            "active_list": True,
            "manual_items": True,
        },
    },
    "custom": {
        "label": "Custom",
        "interval": 5,
        "max_wire_bytes": 1024,
        "toggles": {
            "run_count": True,
            "done_count": True,
            "need_response": True,
            "active_list": True,
            "manual_items": True,
        },
    },
}

TASK_RE = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<title>.+)$")
SECTION_RE = re.compile(r"^##+\s+(?P<title>.+?)\s*$")
DONE_APPROVAL_STATUSES = {"done", "resolved", "closed", "approved", "rejected", "dismissed"}
COMPLETED_STATUSES = {"success"}
COMPLETED_LOOKBACK_HOURS = 24
CHAT_RUNNING_STALE_MINUTES = 120
AWAITING_RESPONSE_STALE_DAYS = 3
THREAD_SCAN_LIMIT = 500
MAX_ROLLOUT_TAIL_BYTES = 1_000_000
CODEX_STATE_DB = "state_5.sqlite"
THREAD_TERMINAL_EVENTS = {"task_complete", "turn_aborted", "error"}
APPROVAL_EVENT_HINTS = ("approval", "approve", "permission_request")
AWAITING_RESPONSE_TOOL_NAMES = {"request_user_input"}
PLAN_AWAITING_MARKERS = (
    "please implement this plan",
    "implement this plan",
    "## summary",
    "## key changes",
    "## test plan",
    "## assumptions",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a compact Codex/Pandora status snapshot for a USB display board.",
    )
    parser.add_argument("--root", help="Avatar Node repo root. Defaults to auto-detect from cwd.")
    parser.add_argument("--now", help="ISO timestamp override, mainly for tests.")
    parser.add_argument("--serial", help=f"Serial device to write to, for example {DEFAULT_SERIAL}.")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate. Default: 115200.")
    parser.add_argument("--watch", action="store_true", help="Keep refreshing instead of running once.")
    parser.add_argument("--interval", type=float, default=5.0, help="Refresh interval in seconds for --watch.")
    parser.add_argument("--dry-run", action="store_true", help="Print the wire payload but do not write serial.")
    parser.add_argument("--quiet", action="store_true", help="Do not print the wire payload to stdout.")
    parser.add_argument("--max-wire-bytes", type=int, default=DEFAULT_MAX_WIRE_BYTES)
    parser.add_argument("--project-name", action="append", default=[], help="Extra whitelisted project name to scan for.")
    parser.add_argument("--mark-seen", action="store_true", help="Legacy: mark current completed chat runs as viewed and exit.")
    parser.add_argument("--http", action="store_true", help="Serve the wire payload over HTTP for WiFi display mode.")
    parser.add_argument("--http-host", default="0.0.0.0", help="HTTP bind host for --http. Default: 0.0.0.0.")
    parser.add_argument("--http-port", type=int, default=8787, help="HTTP port for --http. Default: 8787.")
    return parser.parse_args()


def candidate_roots(explicit_root: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root).expanduser())
    for env_name in ("PANDORA_NODE_ROOT", "ROBIN_AI_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    here = Path.cwd().resolve()
    candidates.extend([here, *here.parents])
    candidates.extend(Path(__file__).resolve().parents)
    return candidates


def resolve_project_root(explicit_root: str | None = None) -> Path:
    seen: set[Path] = set()
    for candidate in candidate_roots(explicit_root):
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "AGENTS.md").exists() and (resolved / "local-runtime").exists():
            return resolved
    raise SystemExit("Could not locate the Avatar Node root. Pass --root explicitly.")


def parse_now(raw: str | None) -> dt.datetime:
    if raw:
        return dt.datetime.fromisoformat(raw)
    return dt.datetime.now().astimezone()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def write_json(path: Path, payload: Any, *, ascii_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=ascii_only, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def state_dir(root: Path) -> Path:
    return root / "local-runtime" / STATE_DIR_NAME


def state_paths(root: Path) -> dict[str, Path]:
    base = state_dir(root)
    return {
        "base": base,
        "status": base / "status.json",
        "app_config": base / APP_CONFIG_NAME,
        "manual_approvals": base / "manual-approvals.json",
        "seen_completions": base / "seen-completions.json",
        "seen_chat_completions": base / "seen-chat-completions.json",
        "log": base / "status.log.jsonl",
    }


def ensure_state(root: Path) -> dict[str, Path]:
    paths = state_paths(root)
    paths["base"].mkdir(parents=True, exist_ok=True)
    if not paths["manual_approvals"].exists():
        write_json(paths["manual_approvals"], {"approvals": []})
    if not paths["seen_completions"].exists():
        write_json(paths["seen_completions"], {"seen": []})
    if not paths["seen_chat_completions"].exists():
        write_json(paths["seen_chat_completions"], {"seen": []})
    if not paths["log"].exists():
        paths["log"].write_text("", encoding="utf-8")
    return paths


def lan_ip() -> str:
    for command in (["ipconfig", "getifaddr", "en0"], ["ipconfig", "getifaddr", "en1"]):
        try:
            value = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            continue
        if value:
            return value
    try:
        output = subprocess.check_output(["hostname", "-I"], text=True, stderr=subprocess.DEVNULL)
        first = output.split()[0]
        if first:
            return first
    except Exception:
        pass
    return "127.0.0.1"


def default_status_url(port: int = 8787) -> str:
    return f"http://{lan_ip()}:{port}/wire"


def preset_defaults(preset_id: str) -> dict[str, Any]:
    preset = DISPLAY_PRESETS.get(preset_id) or DISPLAY_PRESETS["legacy_stable"]
    return {
        "preset": preset_id if preset_id in DISPLAY_PRESETS else "legacy_stable",
        "interval": int(preset["interval"]),
        "max_wire_bytes": int(preset["max_wire_bytes"]),
        "toggles": dict(preset["toggles"]),
        "manual_items": [],
        "name_overrides": {},
    }


def default_app_config(root: Path, *, port: int = 8787) -> dict[str, Any]:
    profile = DEVICE_PROFILES["legacy"]
    display = preset_defaults(str(profile["preset"]))
    return {
        "device_id": profile["id"],
        "codex_home": str(codex_home()),
        "repo_root": str(root),
        "wifi": {
            "ssid": "",
            "password": "",
            "status_url": default_status_url(port),
        },
        "display": display,
        "firmware": {
            "serial_port": profile["default_port"],
            "baud": 115200,
        },
    }


def merge_dict(defaults: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return defaults
    merged = dict(defaults)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_app_config(root: Path, raw: Any, *, existing: dict[str, Any] | None = None, port: int = 8787) -> dict[str, Any]:
    config = merge_dict(default_app_config(root, port=port), raw if isinstance(raw, dict) else {})
    device_id = str(config.get("device_id") or "legacy")
    if device_id not in DEVICE_PROFILES:
        device_id = "legacy"
    config["device_id"] = device_id
    profile = DEVICE_PROFILES[device_id]

    display = merge_dict(preset_defaults(str(profile["preset"])), config.get("display") or {})
    preset_id = str(display.get("preset") or profile["preset"])
    if preset_id != "custom":
        preset = preset_defaults(preset_id)
        preset["manual_items"] = display.get("manual_items") if isinstance(display.get("manual_items"), list) else []
        preset["name_overrides"] = display.get("name_overrides") if isinstance(display.get("name_overrides"), dict) else {}
        display = merge_dict(preset, {"toggles": display.get("toggles") or {}})
    display["interval"] = max(1, int(float(display.get("interval") or profile["interval"])))
    display["max_wire_bytes"] = max(128, int(float(display.get("max_wire_bytes") or profile["max_wire_bytes"])))
    display["toggles"] = merge_dict(preset_defaults(str(display.get("preset") or "legacy_stable"))["toggles"], display.get("toggles") or {})
    display["manual_items"] = normalize_manual_items(display.get("manual_items"))
    display["name_overrides"] = {
        safe_text(key, 80): safe_text(value, 32)
        for key, value in (display.get("name_overrides") or {}).items()
        if safe_text(key, 80) and safe_text(value, 32)
    }
    config["display"] = display

    wifi = merge_dict(default_app_config(root, port=port)["wifi"], config.get("wifi") or {})
    if existing and isinstance(existing.get("wifi"), dict):
        existing_password = str(existing["wifi"].get("password") or "")
        posted_password = str(wifi.get("password") or "")
        if posted_password in {"", "********"} and existing_password:
            wifi["password"] = existing_password
    wifi["ssid"] = str(wifi.get("ssid") or "")
    wifi["password"] = str(wifi.get("password") or "")
    wifi["status_url"] = str(wifi.get("status_url") or default_status_url(port)).strip() or default_status_url(port)
    config["wifi"] = wifi

    firmware = merge_dict(default_app_config(root, port=port)["firmware"], config.get("firmware") or {})
    firmware["serial_port"] = str(firmware.get("serial_port") or profile["default_port"])
    firmware["baud"] = int(float(firmware.get("baud") or 115200))
    config["firmware"] = firmware
    config["codex_home"] = str(Path(str(config.get("codex_home") or codex_home())).expanduser())
    config["repo_root"] = str(Path(str(config.get("repo_root") or root)).expanduser())
    return {
        "device_id": config["device_id"],
        "codex_home": config["codex_home"],
        "repo_root": config["repo_root"],
        "wifi": config["wifi"],
        "display": config["display"],
        "firmware": config["firmware"],
    }


def normalize_manual_items(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for index, item in enumerate(raw[:8]):
        if not isinstance(item, dict):
            continue
        name = safe_text(item.get("name") or item.get("title") or f"manual-{index + 1}", 56)
        if not name:
            continue
        status = safe_text(item.get("status") or "running", 24)
        item_id = safe_text(item.get("id") or f"manual-{index + 1}", 48)
        items.append({"id": item_id, "name": name, "status": status})
    return items


def load_app_config(root: Path, *, port: int = 8787) -> dict[str, Any]:
    paths = ensure_state(root)
    raw = load_json(paths["app_config"], {})
    return normalize_app_config(root, raw, port=port)


def save_app_config(root: Path, payload: Any, *, port: int = 8787) -> dict[str, Any]:
    paths = ensure_state(root)
    existing = load_app_config(root, port=port)
    config = normalize_app_config(root, payload, existing=existing, port=port)
    write_json(paths["app_config"], config)
    return config


def public_app_config(config: dict[str, Any]) -> dict[str, Any]:
    public = json.loads(json.dumps(config))
    wifi = public.setdefault("wifi", {})
    password = str(config.get("wifi", {}).get("password") or "")
    wifi["password"] = "********" if password else ""
    wifi["password_saved"] = bool(password)
    return public


def device_profile(device_id: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(device_id, dict):
        device_id = str(device_id.get("device_id") or "")
    key = str(device_id or "legacy")
    return DEVICE_PROFILES.get(key) or DEVICE_PROFILES["legacy"]


def firmware_dir_for_config(config: dict[str, Any]) -> Path:
    profile = device_profile(config)
    path = FIRMWARE_ROOT / str(profile["firmware_dir"])
    if not path.exists():
        raise FileNotFoundError(f"Firmware directory not found: {path}")
    return path


def arduino_cli() -> str:
    found = shutil.which("arduino-cli")
    if found:
        return found
    bundled = Path("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli")
    return str(bundled) if bundled.exists() else "arduino-cli"


def c_string_escape(value: Any) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def wifi_config_text(config: dict[str, Any]) -> str:
    wifi = config.get("wifi") if isinstance(config.get("wifi"), dict) else {}
    display = config.get("display") if isinstance(config.get("display"), dict) else {}
    interval_ms = max(1000, int(float(display.get("interval") or 5) * 1000))
    return "\n".join(
        [
            "#pragma once",
            "",
            "// Generated by the local Codex Status Display app.",
            "// Keep this file local-only; it contains WiFi credentials.",
            f'#define CODEX_WIFI_SSID "{c_string_escape(wifi.get("ssid"))}"',
            f'#define CODEX_WIFI_PASSWORD "{c_string_escape(wifi.get("password"))}"',
            f'#define CODEX_STATUS_URL "{c_string_escape(wifi.get("status_url"))}"',
            f"#define CODEX_WIFI_POLL_MS {interval_ms}",
            "",
        ]
    )


def write_firmware_wifi_config(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    del root
    firmware_dir = firmware_dir_for_config(config)
    path = firmware_dir / "wifi_config.h"
    text = wifi_config_text(config)
    path.write_text(text, encoding="utf-8")
    return {
        "ok": True,
        "path": str(path),
        "firmware_dir": str(firmware_dir),
        "password_written": bool(str(config.get("wifi", {}).get("password") or "")),
    }


def detect_serial_ports() -> list[dict[str, Any]]:
    patterns = [
        "/dev/cu.usbmodem*",
        "/dev/cu.wchusbserial*",
        "/dev/cu.usbserial*",
        "/dev/tty.usbmodem*",
        "/dev/tty.wchusbserial*",
        "/dev/tty.usbserial*",
    ]
    paths: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            text = str(path)
            if text not in seen:
                seen.add(text)
                paths.append(path)
    defaults = {str(profile["default_port"]): key for key, profile in DEVICE_PROFILES.items()}
    return [
        {
            "path": str(path),
            "name": path.name,
            "exists": path.exists(),
            "recommended_for": defaults.get(str(path), ""),
        }
        for path in paths
    ]


def run_command(args: list[str], *, cwd: Path, timeout: int = 120) -> dict[str, Any]:
    started = time.time()
    try:
        result = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True, timeout=timeout, check=False)
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": safe_text(result.stdout, 6000),
            "stderr": safe_text(result.stderr, 6000),
            "duration_seconds": round(time.time() - started, 2),
            "command": redact_command(args),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": str(exc), "command": redact_command(args)}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": safe_text(exc.stdout or "", 6000),
            "stderr": safe_text(exc.stderr or f"Timed out after {timeout}s", 6000),
            "duration_seconds": round(time.time() - started, 2),
            "command": redact_command(args),
        }


def redact_command(args: list[str]) -> list[str]:
    redacted: list[str] = []
    for part in args:
        text = str(part)
        if "password" in text.lower():
            redacted.append("[redacted]")
        else:
            redacted.append(text)
    return redacted


def compile_firmware(config: dict[str, Any], *, timeout: int = 180) -> dict[str, Any]:
    profile = device_profile(config)
    firmware_dir = firmware_dir_for_config(config)
    command = [arduino_cli(), "compile", "--fqbn", str(profile["fqbn"]), str(firmware_dir)]
    result = run_command(command, cwd=firmware_dir, timeout=timeout)
    result.update({"firmware_dir": str(firmware_dir), "fqbn": profile["fqbn"]})
    return result


def upload_port_error(code: str, port: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "error": "Board port was not found. Plug in the board, refresh ports, then choose the detected board on the Upload page.",
        "port": port,
        "detected_ports": detect_serial_ports(),
    }


def upload_firmware(config: dict[str, Any], *, confirm: bool, timeout: int = 180) -> dict[str, Any]:
    if not confirm:
        return {"ok": False, "error": "Upload requires confirm=true before touching the board."}
    profile = device_profile(config)
    firmware_dir = firmware_dir_for_config(config)
    firmware = config.get("firmware") if isinstance(config.get("firmware"), dict) else {}
    port = str(firmware.get("serial_port") or profile["default_port"])
    if not port:
        return upload_port_error("port_missing", port)
    if not Path(port).exists():
        return upload_port_error("port_missing", port)
    compile_result = compile_firmware(config, timeout=timeout)
    if not compile_result.get("ok"):
        compile_result.update(
            {
                "ok": False,
                "code": "compile_failed",
                "error": "Build failed, so upload was not started.",
                "port": port,
            }
        )
        return compile_result
    command = [arduino_cli(), "upload", "--fqbn", str(profile["fqbn"]), "--port", port, str(firmware_dir)]
    result = run_command(command, cwd=firmware_dir, timeout=timeout)
    result.update({"firmware_dir": str(firmware_dir), "fqbn": profile["fqbn"], "port": port, "compile": compile_result})
    combined_output = f"{result.get('stdout', '')} {result.get('stderr', '')}".lower()
    if not result.get("ok") and any(
        phrase in combined_output
        for phrase in ("could not open", "no such file or directory", "port is busy", "failed uploading")
    ):
        result.update(
            {
                "code": "port_unavailable",
                "error": "Board port is busy or unavailable. Close any serial monitor, refresh ports, then try uploading again.",
                "detected_ports": detect_serial_ports(),
            }
        )
    return result


def short_ascii_name(value: Any, fallback: str = "item", limit: int = 18) -> str:
    text = safe_text(value, 80)
    ascii_text = text.encode("ascii", errors="ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_text)
    if not words:
        return safe_text(fallback, limit)
    drop = {"the", "a", "an", "and", "or", "to", "for", "with", "this", "that", "please"}
    useful = [word for word in words if word.lower() not in drop] or words
    joined = " ".join(useful[:3])
    return safe_text(joined or fallback, limit)


def display_name_for_item(item: dict[str, Any], key: str, overrides: dict[str, str], fallback: str) -> str:
    item_id = str(item.get("id") or item.get("key") or "")
    original = safe_text(item.get(key) or fallback, 56)
    if item_id and item_id in overrides:
        return overrides[item_id]
    return short_ascii_name(original, original, 22)


def apply_display_config(snapshot: dict[str, Any], display: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(display, dict):
        return snapshot
    result = json.loads(json.dumps(snapshot))
    toggles = display.get("toggles") if isinstance(display.get("toggles"), dict) else {}
    overrides = display.get("name_overrides") if isinstance(display.get("name_overrides"), dict) else {}

    projects = result.get("projects") if isinstance(result.get("projects"), list) else []
    awaiting = result.get("awaiting") if isinstance(result.get("awaiting"), list) else []
    completed = result.get("completed") if isinstance(result.get("completed"), list) else []

    for item in projects:
        if isinstance(item, dict):
            item["name"] = display_name_for_item(item, "name", overrides, "run")
    for item in awaiting:
        if isinstance(item, dict):
            item["title"] = display_name_for_item(item, "title", overrides, "resp")
    for item in completed:
        if isinstance(item, dict):
            item["title"] = display_name_for_item(item, "title", overrides, "done")

    if not toggles.get("active_list", True):
        projects = []
    if toggles.get("manual_items", True):
        for manual in normalize_manual_items(display.get("manual_items")):
            item = {
                "id": manual["id"],
                "name": short_ascii_name(overrides.get(manual["id"]) or manual["name"], manual["name"], 22),
                "status": manual["status"],
                "source": "manual",
            }
            projects.append(item)

    if not toggles.get("need_response", True):
        awaiting = []
        for item in projects:
            if isinstance(item, dict) and item.get("status") == "awaiting_response":
                item["status"] = "running"
    if not toggles.get("done_count", True):
        completed = []

    result["projects"] = projects[:8]
    result["awaiting"] = awaiting[:8]
    result["completed"] = completed[:8]
    counts = dict(result.get("counts") or {})
    counts["running_projects"] = len(projects) if toggles.get("run_count", True) else 0
    counts["awaiting_response"] = len(awaiting) if toggles.get("need_response", True) else 0
    counts["completed_today"] = len(completed) if toggles.get("done_count", True) else 0
    counts["done_unseen"] = len(completed) if toggles.get("done_count", True) else 0
    result["counts"] = counts

    alerts: list[str] = []
    if toggles.get("need_response", True):
        alerts.extend(safe_text(f"RESP: {item.get('title')}", 74) for item in awaiting[:2] if isinstance(item, dict))
    if toggles.get("active_list", True):
        alerts.extend(safe_text(f"RUN: {item.get('name')}", 74) for item in projects[:2] if isinstance(item, dict))
    if toggles.get("done_count", True):
        alerts.extend(safe_text(f"DONE: {item.get('title')}", 74) for item in completed[:2] if isinstance(item, dict))
    result["alerts"] = alerts[:6]
    result["ui"] = {
        "r": 1 if toggles.get("run_count", True) else 0,
        "d": 1 if toggles.get("done_count", True) else 0,
        "resp": 1 if toggles.get("need_response", True) else 0,
        "l": 1 if (toggles.get("active_list", True) or toggles.get("manual_items", True)) else 0,
    }
    return result


def app_metadata(config: dict[str, Any]) -> dict[str, Any]:
    profile = device_profile(config)
    firmware_dir = FIRMWARE_ROOT / str(profile["firmware_dir"])
    wifi_config = firmware_dir / "wifi_config.h"
    return {
        "profiles": list(DEVICE_PROFILES.values()),
        "presets": DISPLAY_PRESETS,
        "selected_profile": profile,
        "firmware_dir": str(firmware_dir),
        "wifi_config_exists": wifi_config.exists(),
    }


def display_config_key(config: dict[str, Any]) -> str:
    public = {
        "device_id": config.get("device_id"),
        "codex_home": config.get("codex_home"),
        "repo_root": config.get("repo_root"),
        "display": config.get("display"),
    }
    return json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def config_for_api_payload(root: Path, payload: Any, *, port: int) -> dict[str, Any]:
    if isinstance(payload, dict) and "config" in payload:
        payload = payload.get("config")
    existing = load_app_config(root, port=port)
    return normalize_app_config(root, payload if isinstance(payload, dict) else existing, existing=existing, port=port)


def append_rolling_log(path: Path, record: dict[str, Any], max_lines: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [*existing, line][-max_lines:]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def safe_text(value: Any, limit: int = 72) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def response_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("input_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def looks_like_waiting_plan_message(message: Any) -> bool:
    text = response_content_text(message).strip()
    if not text:
        return False
    lower = text.lower()
    if "please implement this plan" in lower or "implement this plan" in lower:
        return True
    marker_count = sum(1 for marker in PLAN_AWAITING_MARKERS if marker in lower)
    return marker_count >= 3 and ("plan" in lower or "计划" in text)


def current_task_list_path(root: Path, now: dt.datetime) -> Path | None:
    preferred = root / "local-runtime" / "task-list" / now.strftime("%Y-%m") / f"task-list-{now.strftime('%Y-%m')}.md"
    if preferred.exists():
        return preferred
    candidates = sorted((root / "local-runtime" / "task-list").glob("*/task-list-*.md"))
    return candidates[-1] if candidates else None


def collect_tasks(root: Path, now: dt.datetime) -> dict[str, Any]:
    path = current_task_list_path(root, now)
    open_tasks: list[dict[str, str]] = []
    done_count = 0
    current_section = ""
    if path and path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            section_match = SECTION_RE.match(raw)
            if section_match:
                current_section = safe_text(section_match.group("title"), 40)
                continue
            match = TASK_RE.match(raw)
            if not match:
                continue
            title = safe_text(match.group("title"), 92)
            if match.group("mark").lower() == "x":
                done_count += 1
            else:
                open_tasks.append({"title": title, "section": current_section or "Tasks"})

    urgent = [item for item in open_tasks if is_urgent_section(item["section"])]
    return {
        "path": str(path) if path else None,
        "open": open_tasks,
        "open_count": len(open_tasks),
        "urgent_count": len(urgent),
        "done_count": done_count,
        "top_urgent": urgent[:4],
        "top_open": open_tasks[:6],
    }


def is_urgent_section(section: str) -> bool:
    lower = section.lower()
    if "不紧急" in section or "not urgent" in lower:
        return False
    return "紧急" in section or "urgent" in lower


def parse_datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def parse_jsonl_timestamp(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    return parse_datetime(text.replace("Z", "+00:00"))


def codex_home(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    raw = os.environ.get("CODEX_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def load_seen_keys(path: Path) -> set[str]:
    payload = load_json(path, {"seen": []})
    raw_seen = payload.get("seen") if isinstance(payload, dict) else payload
    if not isinstance(raw_seen, list):
        return set()
    return {str(item) for item in raw_seen if str(item).strip()}


def collect_automations(root: Path) -> dict[str, Any]:
    state_root = root / "local-runtime" / "automations"
    items: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    if not state_root.exists():
        return {"items": [], "blockers": []}

    for path in sorted(state_root.glob("*/last-run.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        automation_id = safe_text(payload.get("automation_id") or path.parent.name, 48)
        display_name = safe_text(payload.get("display_name") or automation_id, 48)
        status = safe_text(payload.get("status") or "unknown", 24)
        completed_at = safe_text(payload.get("completed_at") or "", 32)
        raw_blockers = payload.get("blockers") or []
        if isinstance(raw_blockers, str):
            raw_blockers = [raw_blockers]
        clean_blockers = [safe_text(item, 80) for item in raw_blockers if str(item).strip()]
        summary = safe_text(payload.get("summary") or "", 96)
        item = {
            "id": automation_id,
            "name": display_name,
            "status": status,
            "completed_at": completed_at,
            "blockers": clean_blockers,
            "summary": summary,
        }
        items.append(item)
        status_lower = status.lower()
        has_blocker_state = status_lower in {"blocked", "failed", "failure", "error"} or (
            clean_blockers and status_lower not in {"success", "noop"}
        )
        if has_blocker_state:
            reason = clean_blockers[0] if clean_blockers else f"status={status}"
            blockers.append({"name": display_name, "reason": reason})
    return {"items": items, "blockers": blockers}


def completion_key(item: dict[str, Any]) -> str:
    return f"{item.get('id', '')}|{item.get('completed_at', '')}"


def load_seen_completion_keys(root: Path) -> set[str]:
    paths = ensure_state(root)
    payload = load_json(paths["seen_completions"], {"seen": []})
    raw_seen = payload.get("seen") if isinstance(payload, dict) else payload
    if not isinstance(raw_seen, list):
        return set()
    return {str(item) for item in raw_seen if str(item).strip()}


def completed_runs(automations: dict[str, Any], now: dt.datetime, root: Path) -> list[dict[str, str]]:
    seen = load_seen_completion_keys(root)
    cutoff = now - dt.timedelta(hours=COMPLETED_LOOKBACK_HOURS)
    runs: list[tuple[dt.datetime, dict[str, str]]] = []
    for item in automations.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() not in COMPLETED_STATUSES:
            continue
        completed_at = parse_datetime(item.get("completed_at"))
        if not completed_at:
            continue
        comparable_now = now
        if comparable_now.tzinfo is None:
            comparable_now = comparable_now.replace(tzinfo=completed_at.tzinfo or dt.timezone.utc)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=comparable_now.tzinfo or dt.timezone.utc)
        comparable_cutoff = cutoff
        if comparable_cutoff.tzinfo is None:
            comparable_cutoff = comparable_cutoff.replace(tzinfo=completed_at.tzinfo or dt.timezone.utc)
        if completed_at < comparable_cutoff:
            continue
        key = completion_key(item)
        if key in seen:
            continue
        runs.append(
            (
                completed_at,
                {
                    "id": safe_text(item.get("id"), 48),
                    "title": safe_text(item.get("name") or item.get("id") or "completed", 48),
                    "status": "done",
                    "completed_at": safe_text(item.get("completed_at"), 32),
                    "summary": safe_text(item.get("summary"), 64),
                    "key": key,
                },
            )
        )
    runs.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in runs]


def mark_completed_seen(root: Path, automations: dict[str, Any], now: dt.datetime) -> int:
    paths = ensure_state(root)
    existing = load_seen_completion_keys(root)
    visible = completed_runs(automations, now, root)
    for item in visible:
        key = str(item.get("key") or "")
        if key:
            existing.add(key)
    write_json(paths["seen_completions"], {"seen": sorted(existing)})
    return len(visible)


def thread_display_name(row: dict[str, Any]) -> str:
    nickname = safe_text(row.get("agent_nickname"), 24)
    title = safe_text(row.get("title") or row.get("id") or "chat", 44)
    generic_titles = {"chat", "thread", str(row.get("id") or "")}
    if title and title not in generic_titles:
        return title
    if nickname:
        return nickname
    return title


def thread_rows_from_codex(limit: int = THREAD_SCAN_LIMIT, codex_home_path: str | Path | None = None) -> list[dict[str, Any]]:
    home = codex_home(codex_home_path)
    db_path = home / CODEX_STATE_DB
    if not db_path.exists():
        return []
    query = """
        select id, title, rollout_path, updated_at, archived, agent_nickname, agent_role
        from threads
        where archived = 0
        order by updated_at desc
        limit ?
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        with con:
            return [dict(row) for row in con.execute(query, (limit,))]
    except sqlite3.Error:
        return []


def thread_event_summary(rollout_path: Any) -> dict[str, Any]:
    path = Path(str(rollout_path or "")).expanduser()
    summary: dict[str, Any] = {
        "last_status_event": None,
        "last_status_at": None,
        "last_turn_id": "",
        "last_completion_at": None,
        "pending_approval": False,
        "pending_response": False,
        "pending_response_at": None,
        "pending_plan_review": False,
        "pending_plan_review_at": None,
        "last_user_at": None,
        "last_task_started_at": None,
    }
    if not path.exists():
        return summary
    pending_response_calls: dict[str, dt.datetime | None] = {}
    for line in recent_jsonl_lines(path):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_type = str(record.get("type") or "")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        event_at = parse_jsonl_timestamp(record.get("timestamp"))
        if record_type == "event_msg":
            event_type = str(payload.get("type") or "")
            turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
            if event_type == "user_message":
                summary.update(
                    {
                        "last_user_at": event_at,
                        "pending_plan_review": False,
                        "pending_plan_review_at": None,
                    }
                )
            elif event_type == "agent_message" and looks_like_waiting_plan_message(payload.get("message")):
                summary.update(
                    {
                        "pending_plan_review": True,
                        "pending_plan_review_at": event_at,
                    }
                )
            elif event_type == "task_started":
                pending_response_calls.clear()
                summary.update(
                    {
                        "last_status_event": "task_started",
                        "last_status_at": event_at,
                        "last_turn_id": turn_id,
                        "last_task_started_at": event_at,
                        "pending_approval": False,
                        "pending_plan_review": False,
                        "pending_plan_review_at": None,
                    }
                )
            elif event_type == "task_complete":
                pending_response_calls.clear()
                summary.update(
                    {
                        "last_status_event": "task_complete",
                        "last_status_at": event_at,
                        "last_completion_at": event_at,
                        "last_turn_id": turn_id,
                        "pending_approval": False,
                    }
                )
            elif event_type in THREAD_TERMINAL_EVENTS:
                pending_response_calls.clear()
                summary.update(
                    {
                        "last_status_event": event_type,
                        "last_status_at": event_at,
                        "last_turn_id": turn_id,
                        "pending_approval": False,
                    }
                )
            elif any(hint in event_type.lower() for hint in APPROVAL_EVENT_HINTS):
                summary.update(
                    {
                        "last_status_event": event_type,
                        "last_status_at": event_at,
                        "last_turn_id": turn_id,
                        "pending_approval": True,
                    }
                )
        elif record_type == "response_item":
            item_type = str(payload.get("type") or "")
            if item_type == "function_call" and str(payload.get("name") or "") in AWAITING_RESPONSE_TOOL_NAMES:
                call_id = str(payload.get("call_id") or "")
                if call_id:
                    pending_response_calls[call_id] = event_at
            elif item_type == "function_call_output":
                call_id = str(payload.get("call_id") or "")
                pending_response_calls.pop(call_id, None)
            elif item_type == "message":
                role = str(payload.get("role") or "")
                if role == "user":
                    summary.update(
                        {
                            "last_user_at": event_at,
                            "pending_plan_review": False,
                            "pending_plan_review_at": None,
                        }
                    )
                elif role == "assistant" and looks_like_waiting_plan_message(payload.get("content")):
                    summary.update(
                        {
                            "pending_plan_review": True,
                            "pending_plan_review_at": event_at,
                        }
                    )
    pending_response_at = max(
        (when for when in pending_response_calls.values() if isinstance(when, dt.datetime)),
        default=None,
    )
    summary["pending_response"] = bool(pending_response_calls)
    summary["pending_response_at"] = pending_response_at
    plan_at = summary.get("pending_plan_review_at")
    if summary.get("pending_plan_review") and isinstance(plan_at, dt.datetime):
        later_user = summary.get("last_user_at")
        later_start = summary.get("last_task_started_at")
        if (
            isinstance(later_user, dt.datetime)
            and later_user > plan_at
            or isinstance(later_start, dt.datetime)
            and later_start > plan_at
        ):
            summary["pending_plan_review"] = False
            summary["pending_plan_review_at"] = None
    return summary


def normalize_now_for_compare(now: dt.datetime) -> dt.datetime:
    return now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)


def is_same_local_day(value: dt.datetime, now: dt.datetime) -> bool:
    comparable_now = normalize_now_for_compare(now)
    comparable_value = value
    if comparable_value.tzinfo is None:
        comparable_value = comparable_value.replace(tzinfo=comparable_now.tzinfo or dt.timezone.utc)
    else:
        comparable_value = comparable_value.astimezone(comparable_now.tzinfo or dt.timezone.utc)
    return comparable_value.date() == comparable_now.date()


def chat_completion_key(thread_id: str, completed_at: dt.datetime | None) -> str:
    return f"{thread_id}|{completed_at.isoformat() if completed_at else ''}"


def recent_jsonl_lines(path: Path, max_bytes: int = MAX_ROLLOUT_TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            data = handle.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="ignore").splitlines()


def collect_chat_threads(root: Path, now: dt.datetime, codex_home_path: str | Path | None = None) -> dict[str, Any]:
    ensure_state(root)
    home = codex_home(codex_home_path)
    comparable_now = normalize_now_for_compare(now)
    running_cutoff = comparable_now - dt.timedelta(minutes=CHAT_RUNNING_STALE_MINUTES)
    awaiting_cutoff = comparable_now - dt.timedelta(days=AWAITING_RESPONSE_STALE_DAYS)

    running: list[dict[str, str]] = []
    completed_today: list[dict[str, str]] = []
    awaiting_response: list[dict[str, str]] = []
    active_projects: list[dict[str, str]] = []

    for row in thread_rows_from_codex(codex_home_path=home):
        thread_id = str(row.get("id") or "")
        if not thread_id:
            continue
        events = thread_event_summary(row.get("rollout_path"))
        title = thread_display_name(row)
        status_event = events.get("last_status_event")
        status_at = events.get("last_status_at")
        if events.get("pending_response") or events.get("pending_plan_review"):
            pending_at = events.get("pending_response_at") or events.get("pending_plan_review_at")
            if isinstance(pending_at, dt.datetime) and pending_at < awaiting_cutoff:
                continue
            waiting_item = {
                "title": title,
                "status": "awaiting_response",
                "source": "codex-chat",
                "id": thread_id,
            }
            if isinstance(pending_at, dt.datetime):
                waiting_item["waiting_since"] = pending_at.isoformat()
            awaiting_response.append(waiting_item)
            active_projects.append({"name": title, "status": "awaiting_response", "id": thread_id})
            continue
        if status_event == "task_started" and isinstance(status_at, dt.datetime) and status_at >= running_cutoff:
            running_item = {"name": title, "status": "running", "id": thread_id}
            running.append(running_item)
            active_projects.append(running_item)
            continue
        completed_at = events.get("last_completion_at")
        if isinstance(completed_at, dt.datetime) and is_same_local_day(completed_at, now):
            key = chat_completion_key(thread_id, completed_at)
            completed_today.append(
                {
                    "title": title,
                    "status": "done",
                    "completed_at": completed_at.isoformat(),
                    "key": key,
                    "id": thread_id,
                }
            )

    return {
        "running": running[:8],
        "active_projects": active_projects[:8],
        "completed_today": completed_today[:24],
        "completed_unseen": completed_today[:24],
        "awaiting_response": awaiting_response[:8],
        "source": str(home / CODEX_STATE_DB),
    }


def mark_chat_completed_seen(root: Path, now: dt.datetime) -> int:
    paths = ensure_state(root)
    existing = load_seen_keys(paths["seen_chat_completions"])
    chat = collect_chat_threads(root, now)
    for item in chat["completed_today"]:
        key = str(item.get("key") or "")
        if key:
            existing.add(key)
    write_json(paths["seen_chat_completions"], {"seen": sorted(existing)})
    return len(chat["completed_today"])


def collect_promote_reviews(root: Path) -> dict[str, Any]:
    path = root / "local-runtime" / "automations" / "v2-outputs-eywa-promote" / "latest-candidates.json"
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        return {"path": str(path), "needs_review_count": 0, "items": []}
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    review_items: list[dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision") or item.get("status") or "").strip()
        if decision != "needs_review":
            continue
        source = safe_text(item.get("rel_source") or item.get("source") or "review item", 68)
        reason = safe_text(item.get("reason") or "needs review", 84)
        review_items.append({"source": source, "reason": reason})
    return {
        "path": str(path),
        "needs_review_count": len(review_items),
        "items": review_items[:6],
    }


def collect_manual_approvals(root: Path) -> list[dict[str, str]]:
    paths = ensure_state(root)
    payload = load_json(paths["manual_approvals"], {"approvals": []})
    raw_items = payload.get("approvals") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return []

    approvals: list[dict[str, str]] = []
    for raw in raw_items:
        if isinstance(raw, str):
            title = safe_text(raw, 72)
            if title:
                approvals.append({"title": title, "source": "manual", "status": "open"})
            continue
        if not isinstance(raw, dict):
            continue
        status = safe_text(raw.get("status") or "open", 20).lower()
        if status in DONE_APPROVAL_STATUSES:
            continue
        title = safe_text(raw.get("title") or raw.get("summary") or raw.get("task") or "", 72)
        if not title:
            continue
        approvals.append(
            {
                "title": title,
                "source": safe_text(raw.get("source") or "manual", 36),
                "status": status,
            }
        )
    return approvals[:6]


def project_whitelist(extra_names: list[str]) -> dict[str, tuple[str, ...]]:
    whitelist = dict(DEFAULT_PROJECT_WHITELIST)
    for name in extra_names:
        clean = safe_text(name, 48)
        if clean:
            whitelist[clean] = (clean,)
    return whitelist


def collect_running_projects(extra_names: list[str] | None = None) -> list[dict[str, str]]:
    whitelist = project_whitelist(extra_names or [])
    try:
        output = subprocess.check_output(["ps", "-axo", "command"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []

    running: list[dict[str, str]] = []
    for name, tokens in whitelist.items():
        token_lowers = [token.lower() for token in tokens]
        found = False
        for command in output.splitlines():
            lower = command.lower()
            if "codex_status_display.py" in lower:
                continue
            if any(token in lower for token in token_lowers):
                found = True
                break
        if found:
            running.append({"name": name, "status": "running"})
    return running


def build_snapshot(
    root: Path,
    now: dt.datetime,
    extra_project_names: list[str] | None = None,
    *,
    codex_home_path: str | Path | None = None,
    display_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del extra_project_names
    chat = collect_chat_threads(root, now, codex_home_path=codex_home_path)

    alerts: list[str] = []
    for item in chat["awaiting_response"][:2]:
        alerts.append(safe_text(f"Response needed: {item['title']}", 74))
    for item in chat["running"][:2]:
        alerts.append(safe_text(f"Running: {item['name']}", 74))
    for item in chat["completed_today"][:2]:
        alerts.append(safe_text(f"Done: {item['title']}", 74))

    counts = {
        "awaiting_response": len(chat["awaiting_response"]),
        "running_projects": len(chat["active_projects"]),
        "completed_today": len(chat["completed_today"]),
        "done_unseen": len(chat["completed_today"]),
    }
    snapshot = {
        "schema_version": WIRE_SCHEMA_VERSION,
        "updated_at": now.isoformat(),
        "counts": counts,
        "alerts": alerts[:6],
        "projects": chat["active_projects"][:6],
        "awaiting": chat["awaiting_response"][:6],
        "completed": chat["completed_today"][:6],
        "sources": {
            "codex_threads": chat["source"],
            "seen_chat_completions": str(state_paths(root)["seen_chat_completions"]),
            "completed_day": normalize_now_for_compare(now).date().isoformat(),
        },
    }
    return apply_display_config(snapshot, display_config)


def wire_payload(snapshot: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    limits = [(4, 4, 4, 2), (3, 3, 3, 1), (2, 2, 2, 1), (1, 1, 1, 0), (0, 0, 0, 0)]
    base = {
        "v": WIRE_SCHEMA_VERSION,
        "t": snapshot["updated_at"],
        "counts": snapshot["counts"],
        "ui": snapshot.get("ui", {"r": 1, "d": 1, "resp": 1, "l": 1}),
    }
    for project_limit, awaiting_limit, completed_limit, alert_limit in limits:
        payload = {
            **base,
            "projects": compact_items(snapshot.get("projects"), ("name", "status"), project_limit),
            "awaiting": compact_items(snapshot.get("awaiting"), ("title", "status", "waiting_since"), awaiting_limit),
            "completed": compact_items(snapshot.get("completed"), ("title", "status", "completed_at"), completed_limit),
            "alerts": snapshot.get("alerts", [])[:alert_limit],
        }
        if len(encode_wire_line(payload)) <= max_bytes:
            return payload
    return {"v": WIRE_SCHEMA_VERSION, "t": snapshot["updated_at"], "counts": snapshot["counts"]}


def encode_wire_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def compact_items(items: Any, fields: tuple[str, ...], limit: int) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    compacted: list[dict[str, str]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append({field: safe_text(item.get(field), 56) for field in fields if str(item.get(field) or "").strip()})
    return compacted


def render_wire_line(snapshot: dict[str, Any], max_bytes: int = DEFAULT_MAX_WIRE_BYTES) -> str:
    payload = wire_payload(snapshot, max_bytes)
    encoded = encode_wire_line(payload)
    if len(encoded) > max_bytes:
        raise ValueError(f"Wire payload is {len(encoded)} bytes, over the {max_bytes} byte limit")
    return encoded.decode("ascii")


def cached_snapshot(root: Path, now: dt.datetime, max_age_seconds: float) -> dict[str, Any] | None:
    payload = load_json(state_paths(root)["status"], {})
    if not isinstance(payload, dict) or not payload:
        return None
    updated_at = parse_datetime(payload.get("updated_at"))
    if not updated_at:
        return None
    comparable_now = normalize_now_for_compare(now)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=comparable_now.tzinfo or dt.timezone.utc)
    if comparable_now - updated_at > dt.timedelta(seconds=max_age_seconds):
        return None
    return payload


def stale_cached_snapshot(root: Path) -> dict[str, Any] | None:
    payload = load_json(state_paths(root)["status"], {})
    if not isinstance(payload, dict) or not payload:
        return None
    return payload


def fresh_or_built_snapshot(
    root: Path,
    now: dt.datetime,
    args: argparse.Namespace,
    app_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if app_config:
        cache_key = display_config_key(app_config)
        max_age = max(float(app_config.get("display", {}).get("interval") or getattr(args, "interval", 10.0)) * 2.5, 3.0)
        cached = cached_snapshot(root, now, max_age)
        if cached and cached.get("sources", {}).get("display_config_key") == cache_key:
            return cached
        stale = stale_cached_snapshot(root)
        if stale and stale.get("sources", {}).get("display_config_key") == cache_key:
            return stale
        snapshot = build_snapshot(
            Path(str(app_config.get("repo_root") or root)).expanduser(),
            now,
            args.project_name,
            codex_home_path=app_config.get("codex_home"),
            display_config=app_config.get("display") if isinstance(app_config.get("display"), dict) else None,
        )
        snapshot.setdefault("sources", {})["display_config_key"] = cache_key
        write_json(state_paths(root)["status"], snapshot)
        return snapshot
    max_age = max(float(getattr(args, "interval", 10.0)) * 2.5, 15.0)
    snapshot = cached_snapshot(root, now, max_age)
    if snapshot is not None:
        return snapshot
    snapshot = build_snapshot(root, now, args.project_name)
    write_json(state_paths(root)["status"], snapshot)
    return snapshot


def build_and_persist_line(root: Path, now: dt.datetime, args: argparse.Namespace, *, serial_label: str, dry_run: bool) -> str:
    paths = ensure_state(root)
    app_config = load_app_config(root, port=int(getattr(args, "http_port", 8787)))
    snapshot = build_snapshot(
        Path(str(app_config.get("repo_root") or root)).expanduser(),
        now,
        args.project_name,
        codex_home_path=app_config.get("codex_home"),
        display_config=app_config.get("display") if isinstance(app_config.get("display"), dict) else None,
    )
    snapshot.setdefault("sources", {})["display_config_key"] = display_config_key(app_config)
    write_json(paths["status"], snapshot)
    max_wire_bytes = int(app_config.get("display", {}).get("max_wire_bytes") or args.max_wire_bytes)
    line = render_wire_line(snapshot, max_wire_bytes)
    append_rolling_log(
        paths["log"],
        {
            "updated_at": snapshot["updated_at"],
            "counts": snapshot["counts"],
            "wire_bytes": len(line.encode("ascii")),
            "serial": serial_label,
            "dry_run": dry_run,
        },
    )
    return line


class StatusHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        root: Path = self.server.root  # type: ignore[attr-defined]
        args: argparse.Namespace = self.server.args  # type: ignore[attr-defined]
        if path == "/health":
            self.send_json({"ok": True})
            return
        if path == "/api/config":
            config = load_app_config(root, port=int(args.http_port or self.server.server_port))
            self.send_json({"config": public_app_config(config), **app_metadata(config)})
            return
        if path == "/api/status":
            self.send_json(self.api_status_payload(root, args))
            return
        if path == "/api/ports":
            self.send_json({"ports": detect_serial_ports(), "profiles": list(DEVICE_PROFILES.values())})
            return
        if path == "/app" or path.startswith("/app/"):
            self.serve_app(path)
            return
        now = parse_now(args.now)
        if path == "/status":
            snapshot = fresh_or_built_snapshot(root, now, args)
            self.send_json(snapshot, ascii_only=False)
            return
        if path not in {"/", "/wire"}:
            self.send_not_found(path)
            return
        app_config = load_app_config(root, port=int(args.http_port or self.server.server_port))
        snapshot = fresh_or_built_snapshot(root, now, args, app_config)
        max_wire_bytes = int(app_config.get("display", {}).get("max_wire_bytes") or args.max_wire_bytes)
        line = render_wire_line(snapshot, max_wire_bytes)
        append_rolling_log(
            state_paths(root)["log"],
            {
                "updated_at": snapshot["updated_at"],
                "counts": snapshot["counts"],
                "wire_bytes": len(line.encode("ascii")),
                "serial": "http",
                "dry_run": False,
            },
        )
        self.send_bytes(line.encode("ascii") + b"\n", "application/json; charset=ascii")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        root: Path = self.server.root  # type: ignore[attr-defined]
        args: argparse.Namespace = self.server.args  # type: ignore[attr-defined]
        port = int(args.http_port or self.server.server_port)
        body = self.read_json_body()
        if path == "/api/config":
            config = save_app_config(root, body, port=port)
            self.send_json({"ok": True, "config": public_app_config(config), **app_metadata(config)})
            return
        if path == "/api/preview":
            config = config_for_api_payload(root, body, port=port)
            self.send_json({"ok": True, **self.preview_payload(root, args, config)})
            return
        if path == "/api/firmware/write-config":
            config = config_for_api_payload(root, body, port=port)
            save_app_config(root, config, port=port)
            try:
                result = write_firmware_wifi_config(root, config)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
                return
            self.send_json({**result, "config": public_app_config(config)})
            return
        if path == "/api/firmware/compile":
            config = config_for_api_payload(root, body, port=port)
            save_app_config(root, config, port=port)
            self.send_json(compile_firmware(config))
            return
        if path == "/api/firmware/upload":
            config = config_for_api_payload(root, body, port=port)
            confirm = bool(body.get("confirm")) if isinstance(body, dict) else False
            save_app_config(root, config, port=port)
            self.send_json(upload_firmware(config, confirm=confirm))
            return
        if path == "/api/bridge/restart":
            config = config_for_api_payload(root, body, port=port)
            save_app_config(root, config, port=port)
            display = config.get("display") if isinstance(config.get("display"), dict) else {}
            args.interval = float(display.get("interval") or args.interval)
            args.max_wire_bytes = int(display.get("max_wire_bytes") or args.max_wire_bytes)
            self.send_json({"ok": True, "bridge": "applied", **self.api_status_payload(root, args)})
            return
        self.send_not_found(path)

    def log_message(self, fmt: str, *args: Any) -> None:
        server_args: argparse.Namespace = self.server.args  # type: ignore[attr-defined]
        if not server_args.quiet:
            super().log_message(fmt, *args)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def send_json(self, payload: dict[str, Any], *, status: int = 200, ascii_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=ascii_only, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_not_found(self, path: str) -> None:
        if path.startswith("/api/"):
            self.send_json({"ok": False, "error": "not found"}, status=404)
        else:
            self.send_error(404, "not found")

    def serve_app(self, path: str) -> None:
        if not APP_DIST_DIR.exists():
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>Codex Status Display</title>"
                "<body style='font-family:system-ui;padding:32px'>"
                "<h1>Codex Status Display</h1>"
                "<p>The app has not been built yet. Run npm build in services/codex-status-display/app.</p>"
                "</body>"
            ).encode("utf-8")
            self.send_bytes(body, "text/html; charset=utf-8")
            return
        rel = path.removeprefix("/app").lstrip("/")
        target = APP_DIST_DIR / (rel or "index.html")
        if target.is_dir():
            target = target / "index.html"
        try:
            target.resolve().relative_to(APP_DIST_DIR.resolve())
        except ValueError:
            self.send_not_found(path)
            return
        if not target.exists():
            target = APP_DIST_DIR / "index.html"
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_bytes(target.read_bytes(), content_type)

    def preview_payload(self, root: Path, args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
        now = parse_now(args.now)
        snapshot = build_snapshot(
            Path(str(config.get("repo_root") or root)).expanduser(),
            now,
            args.project_name,
            codex_home_path=config.get("codex_home"),
            display_config=config.get("display") if isinstance(config.get("display"), dict) else None,
        )
        max_wire_bytes = int(config.get("display", {}).get("max_wire_bytes") or args.max_wire_bytes)
        payload = wire_payload(snapshot, max_wire_bytes)
        line = encode_wire_line(payload)
        return {
            "snapshot": snapshot,
            "wire": payload,
            "wire_text": line.decode("ascii"),
            "wire_bytes": len(line),
            "max_wire_bytes": max_wire_bytes,
        }

    def api_status_payload(self, root: Path, args: argparse.Namespace) -> dict[str, Any]:
        port = int(args.http_port or self.server.server_port)  # type: ignore[attr-defined]
        config = load_app_config(root, port=port)
        preview = self.preview_payload(root, args, config)
        profile = device_profile(config)
        firmware_dir = FIRMWARE_ROOT / str(profile["firmware_dir"])
        serial_port = str(config.get("firmware", {}).get("serial_port") or profile["default_port"])
        return {
            "ok": True,
            "bridge": {
                "local_url": f"http://127.0.0.1:{self.server.server_port}/wire",  # type: ignore[attr-defined]
                "lan_url": default_status_url(self.server.server_port),  # type: ignore[attr-defined]
                "interval_seconds": int(config.get("display", {}).get("interval") or args.interval),
            },
            "device": {
                "profile": profile,
                "serial_port": serial_port,
                "serial_present": Path(serial_port).exists(),
                "firmware_dir": str(firmware_dir),
                "wifi_config_exists": (firmware_dir / "wifi_config.h").exists(),
            },
            "payload": {
                "wire": preview["wire"],
                "wire_bytes": preview["wire_bytes"],
                "max_wire_bytes": preview["max_wire_bytes"],
            },
            "config": public_app_config(config),
        }

    def send_json_line(self, payload: dict[str, Any], *, ascii_only: bool = True) -> None:
        body = json.dumps(payload, ensure_ascii=ascii_only, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        self.send_bytes(body, "application/json; charset=utf-8")

    def send_bytes(self, body: bytes, content_type: str) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start_http_server(root: Path, args: argparse.Namespace) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((args.http_host, args.http_port), StatusHTTPHandler)
    server.root = root  # type: ignore[attr-defined]
    server.args = args  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not args.quiet:
        print(f"http_status=http://{args.http_host}:{server.server_port}/wire", flush=True)
    return server


def baud_constant(baud: int) -> int:
    name = f"B{baud}"
    if not hasattr(termios, name):
        raise ValueError(f"Unsupported baud rate for termios: {baud}")
    return int(getattr(termios, name))


def configure_serial_fd(fd: int, baud: int = 115200) -> None:
    attrs = termios.tcgetattr(fd)
    speed = baud_constant(baud)
    attrs[4] = speed
    attrs[5] = speed
    attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
    attrs[1] &= ~termios.OPOST
    attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
    attrs[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
    attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


def open_serial_device(device: str, baud: int = 115200) -> int:
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    configure_serial_fd(fd, baud)
    return fd


def write_serial_fd(fd: int, line: str) -> None:
    os.write(fd, line.encode("ascii") + b"\n")
    termios.tcdrain(fd)


def write_serial_line(device: str, line: str, baud: int = 115200) -> None:
    fd = open_serial_device(device, baud)
    try:
        write_serial_fd(fd, line)
    finally:
        os.close(fd)


def run_once(root: Path, now: dt.datetime, args: argparse.Namespace, serial_fd: int | None = None) -> str:
    line = build_and_persist_line(root, now, args, serial_label=args.serial or "", dry_run=bool(args.dry_run or not args.serial))
    if args.serial and not args.dry_run:
        if serial_fd is not None:
            write_serial_fd(serial_fd, line)
        else:
            write_serial_line(args.serial, line, args.baud)
    if not args.quiet:
        print(line, flush=True)
    return line


def main() -> int:
    args = parse_args()
    root = resolve_project_root(args.root)
    if args.mark_seen:
        now = parse_now(args.now)
        marked = mark_chat_completed_seen(root, now)
        if not args.quiet:
            print(f"marked_seen={marked}", flush=True)
        return 0
    serial_fd: int | None = None
    httpd: ThreadingHTTPServer | None = None
    try:
        if args.http:
            httpd = start_http_server(root, args)
        if args.watch and args.serial and not args.dry_run:
            serial_fd = open_serial_device(args.serial, args.baud)
        if args.http and not args.watch and not args.serial and not args.dry_run:
            while True:
                time.sleep(3600)
        while True:
            now = parse_now(args.now)
            run_once(root, now, args, serial_fd)
            if not args.watch:
                return 0
            time.sleep(max(args.interval, 1.0))
    finally:
        if serial_fd is not None:
            os.close(serial_fd)
        if httpd is not None:
            httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
