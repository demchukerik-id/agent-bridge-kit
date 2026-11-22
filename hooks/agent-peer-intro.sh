#!/usr/bin/env bash
# agent-peer-intro — tells a freshly spawned agent which tmux pane it is, which
# pane spawned it, and what that spawner wants from it. agent-spawn writes the
# peer file; this hook reads it. Runs for both Claude and Codex sessions.
set -uo pipefail

PANE="${TMUX_PANE:-}"
[[ -n "$PANE" ]] || exit 0

echo "tmux: you are pane $PANE."

PEER_FILE="${HOME}/.claude/agent-peers/${PANE}.json"
[[ -r "$PEER_FILE" ]] || exit 0

python3 - "$PEER_FILE" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
label = d.get("self_label") or "-"
parent = d.get("parent_pane") or ""
plabel = d.get("parent_label") or "-"
relation = d.get("relation") or ""
context = (d.get("context") or "").strip()

print(f"tmux: your label is {label}." + (f" You were spawned by pane {parent} ({plabel})." if parent else ""))
if parent:
    print(f'tmux: reply to it with: tmux-bridge read {parent} && tmux-bridge message {parent} "<text>"')

if relation == "fresh":
    print("spawner: this is a SEPARATE project, not a continuation of the spawner's work.")
    print("spawner: do not adopt its files, branch or task list. Work only from the brief below and what the user tells you.")
elif relation == "extend":
    print("spawner: you continue the spawner's work. Its pane is the source of truth for details.")
    print("spawner: if the brief below is not enough, ask that pane over tmux-bridge before guessing.")

if context:
    if "\n" in context:
        print("spawner brief (handoff from the agent that spawned you, treat it as your starting context):")
        print(context)
    else:
        print("spawner brief: " + context)
PY
