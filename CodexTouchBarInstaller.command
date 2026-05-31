#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$SCRIPT_DIR/payload"
BIN_SOURCE="$SCRIPT_DIR/bin/codex-touchbar"
INSTALL_ROOT="$HOME/Library/Application Support/CodexTouchBar"
USER_BIN_DIR="$HOME/.local/bin"
USER_CMD_LINK="$USER_BIN_DIR/codex-touchbar"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Please install Python 3 first." >&2
  exit 1
fi

if [[ ! -d "$PAYLOAD_DIR" || ! -x "$BIN_SOURCE" ]]; then
  echo "Installer payload is incomplete." >&2
  exit 1
fi

echo "Installing Codex TouchBar runtime to: $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "$PAYLOAD_DIR/" "$INSTALL_ROOT/"
else
  rm -rf "$INSTALL_ROOT/services" "$INSTALL_ROOT/local-service-wrappers" "$INSTALL_ROOT/local-runtime" "$INSTALL_ROOT/AGENTS.md"
  cp -R "$PAYLOAD_DIR/"* "$INSTALL_ROOT/"
fi

mkdir -p "$INSTALL_ROOT/bin"
cp "$BIN_SOURCE" "$INSTALL_ROOT/bin/codex-touchbar"
chmod +x "$INSTALL_ROOT/bin/codex-touchbar"
chmod +x "$INSTALL_ROOT/local-service-wrappers/services/codex-status-display/touchbar_status_widget.sh"
chmod +x "$INSTALL_ROOT/local-service-wrappers/services/codex-status-display/touchbar_approve_action.sh"

mkdir -p "$USER_BIN_DIR"
ln -sf "$INSTALL_ROOT/bin/codex-touchbar" "$USER_CMD_LINK"

"$INSTALL_ROOT/bin/codex-touchbar" connect --codex-home "$HOME/.codex" >/dev/null

echo ""
echo "Install complete."
echo ""
echo "Try now:"
echo "  codex-touchbar status"
echo ""
echo "If command not found, run using absolute path:"
echo "  $INSTALL_ROOT/bin/codex-touchbar status"
echo ""
echo "Then copy BTT scripts from:"
echo "  codex-touchbar btt-commands"
