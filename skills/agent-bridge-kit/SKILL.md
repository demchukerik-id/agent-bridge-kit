---
name: agent-bridge-kit
description: Run the agent-bridge-kit on this PC - open a new Claude agent in its own VS Code tab, reopen a past Claude conversation, rename an agent, and set up or repair the browser bridge (this PC's own token, Chrome profiles). Use when the user says "open a new agent", "spawn claude", "resume that chat", "rename this agent", "rename the tabs", "set up the browser bridge", "which token", "make a new browser profile", or a bridge command fails.
---

# agent-bridge-kit

Everything here runs in WSL inside tmux. The repo is wherever the user cloned it;
`readlink -f "$(command -v agent-spawn)"` shows it.

## Agents

| Task | Command |
|---|---|
| New Claude agent in a VS Code tab | `agent-spawn claude [dir] ["brief"]`, or the `/create-claude` command |
| Reopen a past conversation | `agent-resume claude "<part of title>"`, `--last`, `--list`; or `/resume-claude` |
| Rename an agent | `agent-rename <pane> <new-name>`, e.g. `agent-rename %12 docs-writer` |
| List agents | `tmux list-panes -a -F '#{pane_id} #{session_name} #{pane_title}'` |

- Your own pane is `$TMUX_PANE`. Renaming yourself queues `/rename` until your turn ends;
  that line prints `QUEUED`, which is fine.
- `agent-rename` sets three labels (Claude's session name, the tmux pane title, the VS Code
  tab) and prints PASS / FAIL / QUEUED for each. Exit code non-zero means a FAIL: report it,
  do not call the rename done.
- Names: no leading `%`, under ~30 characters, no quotes.
- If no tab opens: the Agent Tabs extension needs VS Code reloaded once after install
  (Ctrl+Shift+P, `Developer: Reload Window`). Then `agent-spawn retab <session>`.

## Browser bridge token

The token is this PC's own secret, made by `install.sh`:

```bash
cat ~/.config/claude-web-bridge/token
```

- It is shared only by this PC's bridge server and this PC's Chrome extension. Never use a
  token from anyone else, never paste it into chat, a file in the repo, or a commit.
- The user pastes it into the bridge extension popup (the extension's icon in the debug
  Chrome) in every Chrome profile the bridge drives.
- New token (after a leak, or the popup shows 401):
  `python3 <repo>/server/server.py --rotate-token`, restart with `bridge-ready --restart`,
  and the user pastes the new token into each profile's popup again.

## First browser bridge setup

1. `bridge-ready`. It starts the server, the CDP daemon and a separate debug Chrome
   (its own user-data-dir `%LOCALAPPDATA%\chrome-debug`, apart from the user's daily Chrome).
2. In that Chrome window the user signs in to Google and claude.ai, installs Claude for
   Chrome, then `chrome://extensions` > Developer mode > Load unpacked > `<repo>/ui/extension`
   (from Windows: `\\wsl$\<distro>\home\<user>\<repo>\ui\extension`).
3. The user pastes the token into the bridge popup.
4. Set `"default"` in `~/.config/claude-web-bridge/accounts.json` to that Google account.
5. `bridge-ready` again: every line `[OK  ]`, the last says `READY`.

Test: `browser-bridge open-panel`, then `browser-bridge message /br1 "What is the title of this page?"`.

## New browser profile (only when the user asks)

A profile is a second signed-in account in the same debug Chrome, so the bridge sees it
on the same port.

```bash
bridge-profiles --new        # creates the next "Profile N", opens it, adds it to the group
bridge-profiles --list       # shows each profile and its signed-in account
bridge-profiles              # opens every profile in the group
```

After `--new`, the user does steps 2 and 3 of the first setup in the new window (sign in,
Claude for Chrome, load `ui/extension`, paste the token). Then open panels in it with
`browser-bridge open-panel --account <that email>`. Optional window title per account:
`"window_names": {"<email>": "<title>"}` in `accounts.json`.

## When something fails

`bridge-doctor` checks each part and names the one that failed. Full CLI:
`browser-bridge --help`; more in the `browser-claude-bridge` skill.
