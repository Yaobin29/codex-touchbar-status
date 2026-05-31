# Codex TouchBar Status

A lightweight, installable Touch Bar status kit for Codex users on macOS.

It gives you a clean Touch Bar row with:

- `DONE` count
- `RUN` count (click to expand running chats)
- running chat list (horizontal scroll)
- `APPROVE` indicator and confirm action

## Why this exists

If you run Codex all day, you should not need to keep switching windows just to know:

- what is running,
- what finished,
- and what needs your approval.

This project turns local Codex thread signals into a Touch Bar control surface.

## Quick Install

1. Download or clone this repository.
2. Double-click `CodexTouchBarInstaller.command`.
3. In Terminal, run:

```bash
codex-touchbar connect --codex-home ~/.codex
codex-touchbar status
codex-touchbar btt-commands
```

If `codex-touchbar` is not in your shell PATH yet:

```bash
~/Library/Application\ Support/CodexTouchBar/bin/codex-touchbar status
```

## What gets installed

To:

- `~/Library/Application Support/CodexTouchBar/`

Command shim:

- `~/.local/bin/codex-touchbar`

## BetterTouchTool integration

Use `codex-touchbar btt-commands` and paste the returned paths into BetterTouchTool:

- 4 `Shell Script / Task Widget` items:
  - DONE
  - RUN
  - CHAT
  - APPROVE
- 1 click action script for APPROVE

## One-line prompt for Codex on another Mac

See:

- `docs/CODEX_ONE_LINE_INSTALL_PROMPT.md`

## Demo UI

- `demo/touchbar-ui-preview.html`

## Safety and Privacy

- Uses local files only.
- Reads local Codex state from your machine.
- Does not upload your session content anywhere.

## License

MIT
