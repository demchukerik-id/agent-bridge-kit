---
description: Open a new Claude Code agent in its own VS Code tab, optionally with a handoff from this session
allowed-tools: Bash(agent-spawn:*), Bash(mkdir:*), Bash(git:*), Bash(tmux:*), Bash(cat:*), Bash(sleep:*), Write, Read
---
Spawn a new Claude agent. `$ARGUMENTS` decides how much context it gets.

1. Empty, or only a directory: run `agent-spawn claude [dir]`. No brief.

2. `extend` / `continue` and nothing else: the new agent takes over part of THIS
   session's work. Write a handoff first, from this conversation only, no secrets,
   under ~150 lines: what the project is, the current step, what is done, what to
   watch out for, key files and commands, next actions. Save it to
   `<git root of cwd, else cwd>/handoffs/YYYY-MM-DD-<slug>.md`, then run
   `agent-spawn claude --extend --context-file <absolute path of the handoff>`.

3. `fresh` / `new` and nothing else: a SEPARATE project. Run
   `agent-spawn claude --fresh --context "<one line naming the new project>"`,
   or without `--context` if the user has not said what it is yet.

4. Anything else is a brief: `agent-spawn claude "$ARGUMENTS"`.

Then report in one line: tmux session name, its pane, the directory, and the
handoff path if you wrote one.

## Check it really opened

`agent-spawn` waits up to 20 s for the VS Code tab. Trust its last line:

- `[ok] VS Code tab open ...`: done.
- `[warn] request not picked up`: VS Code or the Agent Tabs extension is not running.
  Ask the user to reload VS Code (Ctrl+Shift+P, `Developer: Reload Window`), then run
  `agent-spawn retab <session>`.
- `[warn] tab opened in a different VS Code window`: ask the user to focus their
  window, then run `agent-spawn retab <session>`.
- `[warn] picked up but no attached tab`: WSL interop is broken if
  `cmd.exe /c echo ok` fails. The user runs `wsl --shutdown` from a Windows
  PowerShell (this restarts WSL and ends every tmux session), then reopens.

With a brief, a background kickoff types one line into the new agent once its
input box is idle. Its status is in `~/.claude/agent-peers/<pane>.kickoff.log`:
`sent` is done, `not-sent: ...` means run `agent-spawn composer <pane>` and type
the brief yourself only if it prints `empty`, `unconfirmed: ...` means never type
it again.

Never report a spawn as done after a `[warn]` without naming the reason.
