# I Turned My MacBook Touch Bar Into a Live Codex Control Surface

## Why I built this

When I am deep in research and development work, I often run multiple Codex tasks in parallel. The biggest friction was not coding itself, it was context switching:

- Which threads are still running?
- Which tasks finished?
- Which ones are blocked waiting for my approval?

I wanted a tiny, always-visible status surface that did not require opening another dashboard window.

So I built **Codex TouchBar Status**: a local, installable setup that maps Codex state to Touch Bar widgets.

## What it shows

The Touch Bar layout is simple and action-oriented:

- `DONE`: how many tasks completed today
- `RUN`: how many active/running threads
- `CHAT`: a horizontally scrollable running list (expand/collapse on RUN)
- `APPROVE`: approval-needed indicator with confirm behavior

There are two interaction ideas I care about:

1. **Progress at a glance**
2. **Fast intervention when approval is needed**

If approvals are pending, the run area auto-expands and the approve button enters an alert state.

## Design principles

I tried to keep the product behavior sharp:

- **No cloud dependency for rendering**
- **Local-first data flow**
- **Low ceremony install**
- **Clear states over fancy visuals**

I also wanted an installation flow that works for non-technical users:

- unzip folder
- run installer
- run one connect command
- paste script paths into BetterTouchTool

## Technical approach

The system reads local Codex state and emits normalized status snapshots.

At a high level:

```text
Local Codex state -> status collector -> status.json -> Touch Bar scripts -> BetterTouchTool widgets
```

### Components

- `codex_status_display.py`
  - collects running/done/awaiting-response signals
  - writes `status.json`
- `touchbar_status_widget.sh`
  - outputs compact widget text for DONE/RUN/CHAT/APPROVE
- `touchbar_approve_action.sh`
  - runs confirm-first approve action
- `codex-touchbar` CLI
  - connect / status / btt-commands / preview
- installer scripts
  - one-click local install and uninstall

## UX behavior highlights

### Expand on demand

The RUN widget behaves like a disclosure control:

- right-pointing triangle in collapsed state
- click to expand running list
- click again to collapse

### Auto-expand on approvals

When approval-needed count is non-zero:

- running list auto-expands
- approve button gets stronger visual emphasis
- collapse is suppressed to avoid missing urgent items

### Horizontal chat navigation

Instead of trying to cram many thread labels into one slot, the chat area is scrollable and snap-aligned.

You can skim 2-3 by default and slide for the rest.

## Why open-source this

I think local AI tooling needs better human interfaces.

Many of us are running sophisticated workflows, but still lack compact control surfaces that fit daily usage patterns. The Touch Bar is imperfect hardware, but it is still a useful micro-display for fast status + action loops.

By open-sourcing this project, I hope others can:

- adapt it to Stream Deck / menu bar / OLED keyboards
- swap Codex signals for other local agent systems
- improve the visual and interaction model

## Installation philosophy

I also optimized for a delegated setup pattern:

You can send this folder to another machine and ask Codex to install/configure it end-to-end with one prompt.

That matters when you manage more than one development machine and want reproducible local automation.

## What I learned

A few practical lessons:

- tiny feedback surfaces reduce cognitive load more than expected
- approve-state signaling is more important than raw running counts
- smooth expand/collapse behavior improves trust in the UI
- minimizing manual setup steps is the difference between "cool demo" and "daily tool"

## What comes next

Potential next steps:

- one-click BetterTouchTool preset import
- menu bar fallback for non-Touch Bar Macs
- optional telemetry-free usage metrics (strictly local)
- richer grouping by project / workspace

## Repo

If you want to try it or fork it:

https://github.com/Yaobin29/codex-touchbar-status

If you build a variant, I would love to see it.
