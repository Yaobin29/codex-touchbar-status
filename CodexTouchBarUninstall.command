#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="$HOME/Library/Application Support/CodexTouchBar"
USER_CMD_LINK="$HOME/.local/bin/codex-touchbar"

echo "Removing Codex TouchBar runtime..."
rm -rf "$INSTALL_ROOT"
if [[ -L "$USER_CMD_LINK" || -f "$USER_CMD_LINK" ]]; then
  rm -f "$USER_CMD_LINK"
fi

echo "Uninstall complete."
