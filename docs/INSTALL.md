# Install Guide (macOS)

## Requirements

- macOS with Touch Bar support (for Touch Bar display)
- Python 3
- BetterTouchTool (for Touch Bar widgets)
- Local Codex state available at `~/.codex` (or custom path)

## Standard install

1. Open this project folder.
2. Run:

```bash
./CodexTouchBarInstaller.command
```

3. Connect to your local Codex home:

```bash
codex-touchbar connect --codex-home ~/.codex
```

4. Validate:

```bash
codex-touchbar status
```

5. Print BetterTouchTool script paths:

```bash
codex-touchbar btt-commands
```

## Fast install via Codex on another Mac

Use the prompt in:

- `docs/CODEX_ONE_LINE_INSTALL_PROMPT.md`

That prompt asks Codex to:

- run installer,
- connect to `~/.codex`,
- verify status,
- configure BetterTouchTool widgets,
- return only final usable results.

## Uninstall

```bash
./CodexTouchBarUninstall.command
```
