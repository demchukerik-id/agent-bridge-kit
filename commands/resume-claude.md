---
description: Reopen a past Claude Code conversation in its own VS Code tab
allowed-tools: Bash(agent-resume:*), Bash(tmux:*)
---
Reopen an earlier Claude conversation. `$ARGUMENTS` picks which one.

- Empty: run `agent-resume claude --list` and show the user the titles, newest first.
  Ask which one, then run step 2 with their answer.
- `last`: run `agent-resume claude --last`.
- Anything else (part of a title, or a session id): run `agent-resume claude "$ARGUMENTS"`.
  If it says no session matches, run `agent-resume claude --list` and show the list.

Add `--dir <folder>` when the user names a project folder; sessions are stored per folder.

Report in one line: the tmux session name and the conversation that was reopened.
