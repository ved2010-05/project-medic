#!/usr/bin/env bash
# run_all.sh — tmux bench-test launcher, the non-systemd way to run all
# four MEDIC processes (design-doc-v0.3.md §8 says launch via tmux OR
# systemd; systemd is scripts/../systemd/, this is the tmux path).
#
# Opens ONE tmux session named "medic" with four panes, one per process,
# so you can watch all four logs live side by side while developing,
# instead of juggling four terminals or four `journalctl -f` windows.
#
#   pane 0 (top-left)     dashboard
#   pane 1 (top-right)    task_bridge
#   pane 2 (bottom-left)  nav
#   pane 3 (bottom-right) ears
#
# Useful tmux keys once attached:
#   Ctrl-b then an arrow key   — move between panes
#   Ctrl-b then z              — zoom the current pane to full-screen, again to undo
#   Ctrl-b then d              — detach (leaves everything running)
#   tmux attach -t medic       — reattach later
#   tmux kill-session -t medic — stop everything and close the session
#
# This script does NOT install anything and does NOT touch systemd — it
# just runs the four processes in the foreground, the same as running
# each by hand in its own terminal (see README.md "Run by hand for bench
# testing" for the one-at-a-time version of this).
#
# BENCH TEST:
#   ./scripts/run_all.sh
#   then Ctrl-b + arrow to check each pane started cleanly (no traceback)
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$INSTALL_DIR"

if ! command -v tmux >/dev/null 2>&1; then
    echo "ERROR: tmux is not installed. Install it with:" >&2
    echo "  sudo apt-get install -y tmux" >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "ERROR: .venv not found at $INSTALL_DIR/.venv" >&2
    echo "Run ./install.sh first — it creates the venv and installs" >&2
    echo "everything this script needs." >&2
    exit 1
fi

if [ ! -f medic.env ]; then
    echo "ERROR: medic.env not found." >&2
    echo "Run ./install.sh first, or copy it by hand:" >&2
    echo "  cp medic.env.example medic.env" >&2
    exit 1
fi

SESSION="medic"
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "A tmux session named '$SESSION' already exists — attaching to it"
    echo "instead of starting a second copy of everything."
    exec tmux attach -t "$SESSION"
fi

# Every pane does the same three things before launching its process:
#   1. cd into the install dir, so `-m medic.xxx` resolves as a package and
#      `dashboard/app.py` resolves as a relative path — exactly like the
#      systemd units' WorkingDirectory=@INSTALL_DIR@.
#   2. export every var from medic.env into that pane's own shell (each
#      tmux pane is a fresh shell, so this has to happen per pane — it is
#      the tmux equivalent of systemd's EnvironmentFile=).
#   3. run the process with the VENV's python, never system python.
RUN_PREFIX="cd '$INSTALL_DIR' && set -a && source medic.env && set +a && "

tmux new-session -d -s "$SESSION" -n medic -x 220 -y 50

# dashboard/app.py, not `-m dashboard.app` — matches how
# medic/task_bridge.py's own bench-test doc runs it by hand.
tmux send-keys -t "$SESSION:0" \
    "${RUN_PREFIX}echo '--- dashboard --- (Ctrl-C to stop just this pane)'; .venv/bin/python dashboard/app.py" C-m

tmux split-window -h -t "$SESSION:0"
tmux send-keys -t "$SESSION:0.1" \
    "${RUN_PREFIX}echo '--- task_bridge ---'; .venv/bin/python -m medic.task_bridge" C-m

tmux split-window -v -t "$SESSION:0.0"
# Small stagger so nav/ears' first log lines land after the dashboard has
# had a moment to bind its port — purely cosmetic, nothing here actually
# depends on ordering (every script is required to tolerate the dashboard
# or MCU being unreachable at startup).
tmux send-keys -t "$SESSION:0.2" \
    "${RUN_PREFIX}echo '--- nav ---'; sleep 1; .venv/bin/python -m medic.nav" C-m

tmux split-window -v -t "$SESSION:0.1"
tmux send-keys -t "$SESSION:0.3" \
    "${RUN_PREFIX}echo '--- ears ---'; sleep 1; .venv/bin/python -m medic.ears" C-m

tmux select-layout -t "$SESSION:0" tiled
tmux attach -t "$SESSION"
