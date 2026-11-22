# agent-bridge-kit

Tools for running Claude Code agents in tmux on Windows + WSL:

- **New agent in a VS Code tab**: `agent-spawn claude` (or `/create-claude`) starts a Claude Code session in its own tmux session and opens it as a VS Code terminal tab. It can hand the new agent a brief.
- **Reopen a past conversation**: `agent-resume claude "<part of its title>"` (or `/resume-claude`).
- **Rename an agent**: `agent-rename <pane> <name>` sets Claude's session name, the tmux pane title and the VS Code tab in one go, and checks each.
- **Browser bridge**: send tasks from the terminal to Claude for Chrome side panels and get the replies back in your tmux pane.

## Requirements

- Windows 10/11 with WSL2 (Ubuntu), and tmux, python3, git and curl in WSL.
- Claude Code in WSL, run inside tmux.
- VS Code on Windows.
- For the browser bridge: Google Chrome, and Python on Windows (`python` on the Windows PATH).
- Optional: [smux](https://github.com/ShawnPana/smux) (`tmux-bridge`), so agents can message each other's panes.

## Install

```bash
git clone <this repo's URL> ~/agent-bridge-kit
cd ~/agent-bridge-kit && ./install.sh
```

The installer:

1. Makes **your own** bridge token in `~/.config/claude-web-bridge/token` (mode 600). Every PC gets a new one; nobody else's token is ever used.
2. Links the commands into `~/.local/bin`.
3. Links `/create-claude`, `/resume-claude` and the skills into `~/.claude`, and adds one SessionStart hook to `~/.claude/settings.json` (a backup is saved next to it). The hook tells a spawned agent which pane it is and gives it its brief.
4. Installs the **Agent Tabs** extension into VS Code and the Chrome debug forwarder into `%LOCALAPPDATA%`.

Then reload VS Code once (Ctrl+Shift+P, `Developer: Reload Window`).

Test from a Claude Code session in tmux:

```bash
agent-spawn claude          # a new tab named claude-pane-N opens
agent-rename %N my-helper   # PASS on all three lines
agent-resume claude --list
```

## Browser bridge setup

1. `bridge-ready`. It starts the server, the CDP daemon and a separate debug Chrome window (its own profile folder, apart from your daily Chrome).
2. In that window: sign in to Google and claude.ai, and install Claude for Chrome.
3. Open `chrome://extensions`, turn on Developer mode, click **Load unpacked** and pick the `ui/extension` folder of this repo (from Windows: `\\wsl$\Ubuntu\home\<you>\agent-bridge-kit\ui\extension`).
4. Open the bridge extension's popup and paste your token: `cat ~/.config/claude-web-bridge/token`.
5. Set `"default"` in `~/.config/claude-web-bridge/accounts.json` to the Google account of that window.
6. Run `bridge-ready` again. Every line says `[OK  ]` and the last one says `READY`.

```bash
browser-bridge open-panel                                   # new tab with a Claude side panel
browser-bridge message /br1 "What is the title of this page?"
browser-bridge list
```

More Google accounts: `bridge-profiles --new` creates another profile in the debug Chrome. Repeat steps 2 to 4 in its window. `bridge-profiles --list` shows them all.

Write messages to a panel the way a person would type them. Claude for Chrome refuses text that looks machine-made.

## Commands

| Command | What it does |
|---|---|
| `agent-spawn claude [dir] ["brief"]` | New agent in a VS Code tab. `--no-tab` for tmux only, `--model <m>`, `retab <session>` to reopen its tab |
| `agent-resume claude <title\|--last\|--list>` | Reopen a past conversation in a new tab |
| `agent-rename <pane> <name>` | Rename the session, the pane title and the tab |
| `browser-bridge open-panel / message / list / read / close-panel` | Drive Claude for Chrome panels |
| `bridge-ready` / `bridge-doctor` | Start the bridge / find the part that failed |
| `bridge-profiles [--new\|--list]` | Open, create or list the debug Chrome profiles |

`agent-spawn codex` and `agent-resume codex` work the same way for the Codex CLI.

## Security

- The token lets anything on this PC drive the debug Chrome through the bridge. Keep it private. To replace it, run `python3 server/server.py --rotate-token` and then `bridge-ready --restart`, and paste the new token into each profile's popup.
- The bridge server and the forwarder accept connections from this PC and WSL only.
- Automated use of claude.ai is a grey area under Anthropic's terms. For production automation, use the Claude API.

## Uninstall

```bash
~/agent-bridge-kit/install.sh --uninstall
rm -rf ~/.config/claude-web-bridge ~/agent-bridge-kit
```

Remove the bridge extension in `chrome://extensions`. To delete the debug Chrome profile too, remove `%LOCALAPPDATA%\chrome-debug`.

## License

MIT
