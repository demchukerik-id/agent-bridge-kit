# Sourced by the bridge's shell scripts, never run directly.
#
# Resolves the Windows user's folders at run time, so the same scripts work on
# any PC whatever the Windows username is (it was "alice" on one machine and
# "Alice Smith", with a space, on the other; hard-coding either broke the
# other). Override with BRIDGE_WIN_LOCALAPPDATA / BRIDGE_WIN_USERPROFILE.
#
#   WIN_LOCALAPPDATA   C:\Users\<name>\AppData\Local
#   WIN_USERPROFILE    C:\Users\<name>
#   WSL_USERPROFILE    /mnt/c/Users/<name>
#   EDITOR_NAME        "VS Code" unless set
#   AGENT_TABS_DIR     the agent-tabs queue (default %USERPROFILE%\.claude\agent-tabs)

EDITOR_NAME="${EDITOR_NAME:-VS Code}"
# BRIDGE_EDITOR_ONLY=1: the caller needs only the editor profile (its AGENT_TABS_DIR is set
# already), so skip the cmd.exe round trip below.
[ "${BRIDGE_EDITOR_ONLY:-}" = 1 ] && return 0

_bridge_win_var() {
  # cd /mnt/c: cmd.exe complains about a UNC cwd when started from a Linux path.
  (cd /mnt/c 2>/dev/null && cmd.exe /c "echo %$1%" 2>/dev/null | tr -d '\r' | tail -n 1)
}

WIN_LOCALAPPDATA="${BRIDGE_WIN_LOCALAPPDATA:-$(_bridge_win_var LOCALAPPDATA)}"
WIN_USERPROFILE="${BRIDGE_WIN_USERPROFILE:-$(_bridge_win_var USERPROFILE)}"
WSL_USERPROFILE="$(wslpath -u "$WIN_USERPROFILE" 2>/dev/null || true)"

if [ -z "$WIN_LOCALAPPDATA" ] || [ "${WIN_LOCALAPPDATA#%}" != "$WIN_LOCALAPPDATA" ]; then
  echo "[bridge] could not resolve the Windows user folder (is cmd.exe on PATH?)." >&2
  echo "[bridge] set BRIDGE_WIN_LOCALAPPDATA='C:\\Users\\<you>\\AppData\\Local' and retry." >&2
fi

AGENT_TABS_DIR="${AGENT_TABS_DIR:-$WSL_USERPROFILE/.claude/agent-tabs}"
