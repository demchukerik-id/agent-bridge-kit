#!/usr/bin/env bash
# agent-bridge-kit installer. Run from WSL as your normal user (not sudo):
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove links, the hook entry and the VS Code extension
#                             (your token and config in ~/.config/claude-web-bridge stay)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-web-bridge"
LOCAL_BIN="$HOME/.local/bin"
CLAUDE_DIR="$HOME/.claude"
HOOK="$CLAUDE_DIR/hooks/agent-peer-intro.sh"
EXT_NAME="local.agent-tabs-0.1.0"

info() { printf '\033[0;32m[kit]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[kit]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31m[kit]\033[0m %s\n' "$*" >&2; exit 1; }

CLIS=(browser-bridge tmux-browser-bridge tmux-browser-list tmux-new-tab
      bridge-ready bridge-up bridge-doctor bridge-profiles
      bridge-open-panel bridge-close-panel
      agent-spawn agent-resume agent-rename)

win_userprofile() {
  case "${BRIDGE_WIN_USERPROFILE:-}" in
    "") ;;
    /*) echo "$BRIDGE_WIN_USERPROFILE"; return ;;
    *)  wslpath -u "$BRIDGE_WIN_USERPROFILE"; return ;;
  esac
  command -v cmd.exe >/dev/null 2>&1 || return 1
  wslpath -u "$(cd /mnt/c && cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r' | tail -n 1)" 2>/dev/null
}

# Add or remove the SessionStart hook entry in ~/.claude/settings.json, keeping everything else.
settings_hook() {
  python3 - "$CLAUDE_DIR/settings.json" "$1" "bash '$HOOK'" <<'PY'
import json, os, shutil, sys
path, mode, cmd = sys.argv[1:]
d = {}
if os.path.exists(path):
    shutil.copy(path, path + ".bak-agent-bridge-kit")
    d = json.load(open(path))
groups = d.setdefault("hooks", {}).setdefault("SessionStart", [])
groups[:] = [g for g in groups if not any("agent-peer-intro.sh" in h.get("command", "") for h in g.get("hooks", []))]
if mode == "add":
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
if not groups:
    del d["hooks"]["SessionStart"]
json.dump(d, open(path, "w"), indent=2)
PY
}

if [[ "${1:-}" == "--uninstall" ]]; then
  for l in "$LOCAL_BIN"/* "$CLAUDE_DIR/commands"/* "$CLAUDE_DIR/skills"/* "$HOOK"; do
    [ -L "$l" ] && [[ "$(readlink "$l")" == "$REPO"/* ]] && rm -f "$l"
  done
  rm -f "$LOCAL_BIN/bridge-daemon"
  [ -f "$CLAUDE_DIR/settings.json" ] && settings_hook remove
  if W="$(win_userprofile)" && [ -d "$W/.vscode/extensions/$EXT_NAME" ]; then
    rm -rf "$W/.vscode/extensions/$EXT_NAME"
  fi
  info "Uninstalled. Config and token left in $CONFIG_DIR (delete it to remove them)."
  exit 0
fi

[[ $EUID -ne 0 ]] || die "run as your normal user, not root"
for c in python3 curl git tmux; do command -v "$c" >/dev/null 2>&1 || die "missing $c (sudo apt install $c)"; done
command -v claude >/dev/null 2>&1 || warn "claude is not on PATH; agent-spawn needs Claude Code installed"
command -v cmd.exe >/dev/null 2>&1 || warn "cmd.exe not found: this kit expects WSL2 on Windows"

# 1. Config and this PC's own token. Never copied from anyone else.
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [[ ! -s "$CONFIG_DIR/token" ]]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$CONFIG_DIR/token")
  info "made a new bridge token: $CONFIG_DIR/token"
else
  info "kept the existing token: $CONFIG_DIR/token"
fi
chmod 600 "$CONFIG_DIR/token"
[[ -f "$CONFIG_DIR/accounts.json" ]] || cp "$REPO/examples/accounts.example.json" "$CONFIG_DIR/accounts.json"
[[ -f "$CONFIG_DIR/panels.json" ]] || echo '{}' > "$CONFIG_DIR/panels.json"

# 2. Commands on PATH.
chmod +x "$REPO"/scripts/* "$REPO"/hooks/* "$REPO/server/server.py" "$REPO/daemon/cdp_daemon.py"
mkdir -p "$LOCAL_BIN"
for f in "${CLIS[@]}"; do ln -sf "$REPO/scripts/$f" "$LOCAL_BIN/$f"; done
ln -sf "$REPO/scripts/daemon-restart" "$LOCAL_BIN/bridge-daemon"
case ":$PATH:" in *":$LOCAL_BIN:"*) ;; *) warn "add to ~/.bashrc:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;; esac

# 3. Claude Code: slash commands, skills and the hook that hands a spawned agent its brief.
mkdir -p "$CLAUDE_DIR/commands" "$CLAUDE_DIR/skills" "$CLAUDE_DIR/hooks"
for f in "$REPO"/commands/*.md; do ln -sf "$f" "$CLAUDE_DIR/commands/$(basename "$f")"; done
for d in "$REPO"/skills/*/; do ln -sfn "${d%/}" "$CLAUDE_DIR/skills/$(basename "$d")"; done
ln -sf "$REPO/hooks/agent-peer-intro.sh" "$HOOK"
settings_hook add
info "linked /create-claude, /resume-claude, the skills and the SessionStart hook"

# 4. Windows side: the CDP forwarder and the VS Code tab extension.
if W="$(win_userprofile)" && [ -d "$W" ]; then
  LAD="$W/AppData/Local"
  if [ -d "$LAD" ]; then
    cp "$REPO/windows/chrome-debug-forwarder.py" "$LAD/chrome-debug-forwarder.py"
  else
    warn "no $LAD; copy windows/chrome-debug-forwarder.py to %LOCALAPPDATA% by hand"
  fi
  EXT="$W/.vscode/extensions/$EXT_NAME"
  mkdir -p "$EXT"
  cp "$REPO"/windows/agent-tabs/{extension.js,package.json,icon.png} "$EXT/"
  mkdir -p "$W/.claude/agent-tabs"
  info "installed the Agent Tabs extension in VS Code (reload VS Code once: Ctrl+Shift+P, Developer: Reload Window)"
else
  warn "could not find the Windows user folder; set BRIDGE_WIN_USERPROFILE='C:\\Users\\<you>' and re-run"
fi

command -v tmux-bridge >/dev/null 2>&1 || warn "optional: install smux (https://github.com/ShawnPana/smux) so agents can message each other with tmux-bridge"

cat <<EOF

Installed. Next:
  1. Reload VS Code once, then from a Claude Code session in tmux try:  agent-spawn claude
  2. Browser bridge: run  bridge-ready  and follow its lines (README.md, "Browser bridge setup").
EOF
