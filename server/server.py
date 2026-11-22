#!/usr/bin/env python3
"""
browser-claude-bridge server.

Routes:
  POST /send    {from_pane, text, mode?, tab_id?, tab_match?} -> enqueue, returns {id}
                  mode = 'tab' (default) | 'sidepanel'
  GET  /poll[?mode=tab|sidepanel]                              -> next outbox item or 204
                  filters queue by mode; default returns 'tab' items
  POST /reply   {id, from_pane?, text}                         -> deliver to tmux pane via tmux-bridge
  POST /tabs    {tabs:[{id,title,url,active}]}                 -> extension pushes its tab list
  GET  /tabs                                                    -> {tabs:[...], updated_at}
  GET/POST /pin {url_match, title_hint?}                       -> manage pinned tab
  GET  /health                                                  -> {ok, queued, pending_replies, tabs}

Auth: Authorization: Bearer <token>
  Token is read from ~/.config/claude-web-bridge/token (created on first run).
"""

import hashlib
import json
import urllib.parse
import urllib.request
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reply_format import structure_reply
import panel_meta
import detectors

PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "7777"))
TOKEN_FILE = Path.home() / ".config" / "claude-web-bridge" / "token"
TMUX_BRIDGE = os.environ.get("TMUX_BRIDGE_BIN", "tmux-bridge")


def _win_userprofile_wsl() -> str:
    """The Windows user folder as a WSL path, e.g. /mnt/c/Users/<name>.

    Resolved at run time rather than hard-coded, because the Windows username
    differs per PC ("alice" on one, "Alice Smith" on another) and a fixed
    path silently broke session lookup and the skills list on the other one.
    Override with BRIDGE_WIN_USERPROFILE (a Windows path).
    """
    win = os.environ.get("BRIDGE_WIN_USERPROFILE", "")
    if not win:
        try:
            win = subprocess.run(["cmd.exe", "/c", "echo %USERPROFILE%"], cwd="/mnt/c",
                                 capture_output=True, text=True, timeout=10
                                 ).stdout.strip().splitlines()[-1].strip()
        except Exception:
            win = ""
    if win and not win.startswith("%"):
        try:
            return subprocess.run(["wslpath", "-u", win], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except Exception:
            pass
    return "/mnt/c/Users/" + os.environ.get("USER", "")


WIN_HOME = _win_userprofile_wsl()
BROWSER_SKILLS_DIR = os.path.join(WIN_HOME, ".claude", "browser-skills")


def _generate_and_write_token() -> str:
    """Generate a fresh token and atomically write to TOKEN_FILE."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok)
    try:
        TOKEN_FILE.chmod(0o600)
    except (PermissionError, OSError):
        # On WSL with NTFS-backed paths, chmod may be a no-op. Not fatal.
        pass
    return tok


def load_token() -> str:
    """Return the persisted token. Never silently regenerate.

    Behaviour:
    - If TOKEN_FILE exists and is non-empty, return its contents.
    - If TOKEN_FILE is missing, this is a first run: generate, write, return,
      print a *first-run* notice (only path the secret ever surfaces).
    - If TOKEN_FILE exists but is empty: refuse to start with a clear error.
      The operator probably truncated it by mistake; silently regenerating
      would break the extension popup's cached token.
    Use `--rotate-token` to deliberately replace the token.
    """
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
        sys.stderr.write(
            f"FATAL: token file {TOKEN_FILE} exists but is empty.\n"
            f"  Either rotate the token (this server with --rotate-token), or\n"
            f"  restore the previous value if you have it saved.\n"
        )
        sys.exit(2)
    # First-run path: file does not exist at all.
    tok = _generate_and_write_token()
    print(
        f"first-run: generated new token at {TOKEN_FILE}",
        file=sys.stderr,
    )
    print(
        f"   sha8={hashlib.sha256(tok.encode()).hexdigest()[:8]}",
        file=sys.stderr,
    )
    print(
        f"   one-time secret: {tok}  (paste into the extension popup now)",
        file=sys.stderr,
    )
    return tok


def _token_sha8(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()[:8]


# CLI: support `--rotate-token` so operators can rotate explicitly without
# editing the file by hand. Any other CLI arg is ignored to keep this compatible
# with existing wrappers.
if "--rotate-token" in sys.argv:
    old = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    new = _generate_and_write_token()
    msg = "rotated" if old else "created"
    print(f"token {msg}.", file=sys.stderr)
    print(f"  old sha8 = {_token_sha8(old) if old else '<none>'}", file=sys.stderr)
    print(f"  new sha8 = {_token_sha8(new)}", file=sys.stderr)
    print(f"  new value: {new}  (paste into the extension popup now)", file=sys.stderr)
    sys.exit(0)


TOKEN = load_token()

# State
outbox: deque = deque()           # items: {id, from_pane, text, tab_id?, tab_match?}
in_flight: dict = {}              # id -> from_pane (waiting for reply)
replies_buffer: dict = {}         # id -> {text, from_agent, ts} for virtual:* panes
tabs_state: dict = {"tabs": [], "updated_at": 0}
last_active_tab: dict = {}        # tab_id -> ts of last /reply received
# tab_id -> {"pct", "input_tokens", "at", "seen"} — context-window usage for a
# browser agent, measured by the extension from the side panel's own API
# traffic (input_tokens of the last turn). Pushed by the extension, read by the
# popup and the dashboard. Memory only: it is a live reading, not state to keep.
ctx_usage: dict = {}
ctx_diag: dict = {}   # last read's diagnostics from the extension
last_reply_per_tab: dict = {}     # tab_id -> {text, ts, len}
streaming_ids: set = set()        # in_flight ids that have started streaming a response
# Browser task progress (BRIDGE-TASKS-SPEC.md, read by sessions-overview via
# GET /tasks). One record per --task id; every number is measured, never
# estimated: step/attempt from the sends, actions from the daemon's read.
tasks: dict = {}                  # task_id -> record
task_by_dispatch: dict = {}       # dispatch id -> task_id
TASK_KEEP_S = 3600                # ended tasks stay listed this long
PIN_FILE = Path.home() / ".config" / "claude-web-bridge" / "pin.json"
AGENTS_FILE = Path.home() / ".config" / "claude-web-bridge" / "agents.json"
PANELS_FILE = Path.home() / ".config" / "claude-web-bridge" / "panels.json"
RELAY_STATE_FILE = Path.home() / ".config" / "claude-web-bridge" / "relay-state.json"
# Persistent pane names: keyed by Claude session UUID so a rename survives
# tmux restarts and `claude --exit` + `claude --resume <uuid>` cycles.
# Format: { "<session_uuid>": "<name>" }. Written every time the popup
# polls /tmux-panes and sees a current @name on a pane with a resolved
# session_id; restored to @name when the pane has a session_id but no @name.
PANE_NAMES_FILE = Path.home() / ".config" / "claude-web-bridge" / "pane-names.json"
pane_names_by_session: dict = {}
# Read-guard state for the smux-style /panel/{type,keys,read} endpoints.
# Mirrors the `tmux-bridge` CLI's read-act-read enforcement: a caller MUST
# GET /panel/read?to=X within READ_GUARD_TTL_S before a type/keys to that
# same panel. The mark is cleared after each successful type/keys.
# Key: (caller, panel_tab_id) -> last_read_at_epoch.
panel_read_marks: dict = {}
READ_GUARD_TTL_S = 30  # generous; tmux-bridge gives no explicit window

# Staged text from /panel/type, keyed by tab_id. /panel/keys with key=Enter
# pops the entry and dispatches a combined insert+click item to the daemon
# (the only flow that survives Claude's React controlled-component model —
# Input.insertText alone gets reverted when the daemon's WS closes).
# Cleared on dispatch OR on next /panel/type for the same tab.
pending_panel_text: dict = {}
# Pending renames written by /tmux-pane/rename, drained by the next
# /tmux-panes poll into pane_names_by_session once the pane's session_id
# is resolvable. {pane_id: (new_name, enqueued_at_epoch)}. GC'd after 60s
# if the pane vanishes before its session is resolved.
pending_renames_by_pane: dict = {}
# Per-pane cache of the most recently resolved session, so the popup can
# still show a 📋 copy-resume command for panes whose claude.exe has
# exited (session file removed). {pane_id: {session_id, cwd, ts}}.
# GC'd after LAST_SESSION_TTL_S of no refresh.
last_session_per_pane: dict = {}
LAST_SESSION_TTL_S = 24 * 60 * 60  # 24h
lock = threading.Lock()
reply_cv = threading.Condition(lock)  # notify waiters when replies_buffer gains an entry


def load_pin() -> dict:
    if not PIN_FILE.exists():
        return {}
    try:
        return json.loads(PIN_FILE.read_text())
    except Exception:
        return {}


def save_pin(pin: dict) -> None:
    PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIN_FILE.write_text(json.dumps(pin))


pin_state: dict = load_pin()  # {} or {"url_match": str, "title_hint": str, "set_at": float}


def load_agents() -> dict:
    if not AGENTS_FILE.exists():
        return {}
    try:
        return json.loads(AGENTS_FILE.read_text())
    except Exception:
        return {}


def load_panels() -> dict:
    """Friendly-name registry for browser side panels.
    Shape: {name: {to: agent_name, tab_id: int}}"""
    if not PANELS_FILE.exists():
        return {}
    try:
        return json.loads(PANELS_FILE.read_text())
    except Exception:
        return {}


def save_panels(d: dict) -> None:
    PANELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PANELS_FILE.write_text(json.dumps(d, indent=2))


def save_agents(d: dict) -> None:
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENTS_FILE.write_text(json.dumps(d, indent=2))


# Agents registry: { name: {profile_dir: str, port: int, registered_at: float, note?: str} }
# Each agent owns a Chrome profile (its own user-data-dir folder) and a unique
# debug port. The daemon connects to that port to drive that agent's side panel.
agents: dict = load_agents()
panels: dict = load_panels()
# Discovered (transient): tab groups Chrome currently has open. Pushed by the
# extension. Shape: { group_title: {tab_id: int, group_id: int, tab_title: str} }
discovered_panels: dict = {}

# When each panel was last announced to a terminal agent, keyed by tab id.
# A timestamp rather than a membership set: several extensions can push to
# /panels/discovered (one per Chrome window) and each push replaces the whole
# discovered_panels map, so a tab can leave and re-enter `new_tab_ids` every
# few seconds. A set that forgot absent tabs re-announced them forever.
# `seeded` guards the first push after a restart, where every open panel looks
# brand new. `last_announce` backs a global throttle so no future bug in this
# path can flood the operator's pane again.
announced_at: dict = {}
discovery_state: dict = {"seeded": False, "last_announce": 0.0}
ANNOUNCE_COOLDOWN_S = 600   # per tab
ANNOUNCE_MIN_GAP_S = 20     # across all tabs

# When a registered panel's tab was first absent from a discovery push, so a
# panel whose tab is really gone stops occupying a /brN slot. Keyed by tab id.
panel_missing_since: dict = {}
PANEL_GRACE_S = 180

# Names, group colors and the 5 h cleanup (notes/agent-system PLAN A3,
# server/panel_meta.py). panel-meta.json is written when open-panel --name
# opens a panel; the sweep deletes chat and group 5 h after that.
PANEL_META_FILE = Path.home() / ".config" / "claude-web-bridge" / "panel-meta.json"
panel_meta_store = panel_meta.MetaStore(PANEL_META_FILE)
PANEL_TTL_S = float(os.environ.get("BRIDGE_PANEL_TTL_S") or panel_meta.TTL_S)
SWEEP_S = 60.0
SWEEP_MAX_DEFERRALS = 3     # delete deferred this many sweeps in a row: one popup
our_color_picks: dict = {}  # color -> when the meta rename last picked it
cleaning_tabs: set = set()  # tabs the sweep is deleting: /poll holds their dispatches
CFC_EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"  # Claude for Chrome
CLOSE_PANEL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge-close-panel"

# Tabs already reported to the operator as not drivable. The extension in a
# normal Chrome profile pushes ITS panels here too, but the daemon can only
# drive side panels inside the chrome-debug profile. Announcing those produced
# a "connected" line followed by a "no Claude side panel" error, over and over.
# One line per tab id, once, then silence.
unreachable_noted: set = set()

# Panel-close notices. A panel announces itself when it opens; when the user
# closes it (or its tab), the bound pane otherwise finds out only when a
# dispatch fails with `no Claude side panel with tabId=...`. These back a
# watcher that turns that into one line, once, in the owning pane.
#   panel_alive_once  — tab ids Chrome has actually shown a side panel for.
#                       Only these can be reported closed, which keeps panels
#                       in other Chrome profiles (never visible over CDP, see
#                       `unreachable_noted`) out of it entirely.
#   panel_short_seen  — the /brN alias captured while the panel was alive. The
#                       registry has usually dropped the tab by the time the
#                       notice is sent, and "tab:1635164807" is not a name the
#                       operator recognises.
#   panel_absent_polls— consecutive SUCCESSFUL CDP probes missing that tab.
#   close_notified    — tab ids already reported. Cleared if the id comes back.
panel_alive_once: set = set()
panel_short_seen: dict = {}
panel_absent_polls: dict = {}
close_notified: set = set()
# Tabs a bridge command is closing on purpose (close-panel, delete-chat,
# delete-tasks). Their close is not announced: the caller already knows, and
# the notices flooded the operator's pane during testing (2026-10-04).
closing_intended: set = set()
# 1s polling: a /json/list against the debug port costs ~8ms and the list does
# not flicker (17 samples, 20s, one panel open and Chrome in use, zero changes).
# Two confirms rather than one still rules out a single transient probe, and
# puts the notice in the pane 1-2s after the close instead of 10-15s.
CLOSE_POLL_S = 1.0
CLOSE_CONFIRM_POLLS = 2     # ~2s of unbroken absence before a notice goes out

# Relay gate — per-tab + a default.
#   - relay_default_enabled: applied to any tab without an explicit override.
#   - relay_overrides: {tab_id: bool} — per-tab overrides set from the popup.
# The watcher passes ?tab_id=N when polling /relay-enabled so it gets a
# resolved answer for that specific tab. The legacy `enabled` body field on
# POST (no tab_id) still flips the default for backward compatibility.
relay_default_enabled: bool = True
relay_overrides: dict = {}        # tab_id -> bool
relay_changed_at: float = 0.0
relay_changed_by: str = "default"

# Relay routing target — per-tab + a default.
# When the watcher pushes a browser-agent reply, this controls which tmux
# pane it gets typed into. Users with multiple terminal agents (orchestrator,
# codex, browser, etc.) can route each browser panel to whichever pane should
# receive its replies. Empty/null override = follow the default.
relay_default_target: str = "orchestrator"
relay_targets: dict = {}          # tab_id -> pane label (string)

# Pending claims for newly-opened tabs: when /browser/open-tab enqueues a
# tabs.create item, we also push a claim here. The NEXT browser panel
# discovered via POST /panels/discovered (within the TTL) auto-gets
# relay_targets[new_tab_id] = from_pane. 5-minute TTL prevents a stale claim
# from grabbing an unrelated panel.
pending_panel_claims: deque = deque()  # items: {from_pane, expires_at}

# Per-pane "last opened tab" tracking. Updated on /browser/open-tab so that
# /browser/rename-last-tab can later rename the most-recent tab from that
# pane without the operator having to remember the tab_id.
# Shape: {from_pane: {"tab_id": int, "opened_at": float}}
last_tab_per_pane: dict = {}

# Chrome's tab-group color palette. Order matters only for the never-used
# "first available" tie-break inside _pick_unique_color().
ALLOWED_TAB_GROUP_COLORS = (
    "grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange",
)

# tab_group_color[tab_id] = {"color": "<palette name>", "assigned_at": float}
# color_lru[color]        = last unix-ts a /browser/rename-last-tab call picked it
# Both are mutated only inside `lock`. The popup reads `tab_group_color` via the
# `color` field on /tmux-panes → connected_browsers so the side-panel dots can
# mirror the Chrome tab-group color.
tab_group_color: dict = {}
color_lru: dict = {}


def _pick_unique_color() -> str:
    """Pick a tab-group color, preferring one not currently assigned.

    Among palette colors not currently in `tab_group_color`, return the one
    whose color_lru entry is oldest (never-used colors win, since their
    implicit ts is 0). If every palette color is already assigned to a live
    tab, fall back to the least-recently-used color globally — duplicates are
    unavoidable past 9 tabs.

    Caller must hold `lock`. Updates color_lru for the chosen color.
    """
    now = time.time()
    in_use = {e.get("color") for e in tab_group_color.values() if e.get("color")}
    available = [c for c in ALLOWED_TAB_GROUP_COLORS if c not in in_use]
    pool = available or list(ALLOWED_TAB_GROUP_COLORS)
    chosen = min(pool, key=lambda c: color_lru.get(c, 0.0))
    color_lru[chosen] = now
    return chosen


# Per-pane prompt/reply activity timestamps.
# Used by GET /tmux-panes to compute a per-pane `state` field that the popup
# colour-codes ("new", "active", "finished", "idle"). Keys are tmux pane ids
# (e.g. "%12"); values are unix timestamps (float, from time.time()).
#
# last_prompt_sent_per_pane is ALSO how announce_new_panel decides which
# terminal agent hears about a newly opened browser panel, so it is persisted:
# when it lived only in memory, a server restart silently blackholed every
# announcement until each agent happened to take another prompt.
HEARTBEAT_FILE = Path.home() / ".config" / "claude-web-bridge" / "pane-heartbeats.json"
# Operator-pinned destination for panel announcements. When set, it wins over
# the most-recently-prompted heuristic, which follows whichever pane the
# operator last typed into and so drifts to whatever they are working on.
ANNOUNCE_PIN_FILE = Path.home() / ".config" / "claude-web-bridge" / "announce-pin.json"
# tab_id -> /brN. Persisted because the mapping only being in memory meant
# a server restart re-seeded it in first-seen order, silently re-pointing
# /br0 at a different panel. A caller dispatching by alias then hit a panel
# it never meant to touch.
SHORTS_FILE = Path.home() / ".config" / "claude-web-bridge" / "panel-shorts.json"


def load_heartbeats() -> dict:
    try:
        return {k: float(v) for k, v in json.loads(HEARTBEAT_FILE.read_text()).items()}
    except Exception:
        return {}


def save_heartbeats(d: dict) -> None:
    try:
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(json.dumps(d))
    except Exception:
        pass


last_prompt_sent_per_pane: dict = load_heartbeats()


def load_announce_pin() -> tuple[str | None, float]:
    """
    (pane, set_at). set_at is when the operator pinned; a heartbeat newer
    than it means the operator has since talked to another agent, and that
    agent, not the pin, should hear about new panels. Files written before
    set_at existed fall back to the file's mtime.
    """
    try:
        d = json.loads(ANNOUNCE_PIN_FILE.read_text())
        pane = d.get("pane") or None
        set_at = float(d.get("set_at") or ANNOUNCE_PIN_FILE.stat().st_mtime)
        return pane, set_at
    except Exception:
        return None, 0.0


def save_announce_pin(pane: str | None) -> None:
    try:
        ANNOUNCE_PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANNOUNCE_PIN_FILE.write_text(json.dumps({"pane": pane, "set_at": time.time()}), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] could not persist announce pin: {e}", file=sys.stderr)


announce_pin, announce_pin_at = load_announce_pin()
last_reply_per_pane: dict = {}         # pane_id -> ts of last reply delivered into that pane


def load_relay_state() -> None:
    """Populate relay defaults + overrides from disk if a state file exists.

    Persisted shape:
      {
        "default_enabled": bool,
        "overrides_enabled": { "<tab_id>": bool },
        "default_target":  "<label>",
        "overrides_target": { "<tab_id>": "<label>" }
      }

    Without this, every server restart resets per-tab popup choices to the
    defaults — which is how the 2026-05-11 ACL test surfaced an apparent
    ownership mismatch (the operator's "agent-a -> error-log" override
    evaporated when the server restarted).
    """
    global relay_default_enabled, relay_overrides
    global relay_default_target, relay_targets
    try:
        if not RELAY_STATE_FILE.exists():
            return
        d = json.loads(RELAY_STATE_FILE.read_text())
        if isinstance(d.get("default_enabled"), bool):
            relay_default_enabled = d["default_enabled"]
        if isinstance(d.get("overrides_enabled"), dict):
            relay_overrides = {int(k): bool(v) for k, v in d["overrides_enabled"].items() if str(k).lstrip("-").isdigit()}
        if isinstance(d.get("default_target"), str) and d["default_target"]:
            relay_default_target = d["default_target"]
        if isinstance(d.get("overrides_target"), dict):
            relay_targets = {int(k): str(v) for k, v in d["overrides_target"].items() if v and str(k).lstrip("-").isdigit()}
    except Exception as e:
        print(f"[relay-state] failed to load {RELAY_STATE_FILE}: {e}", file=sys.stderr)


def save_relay_state() -> None:
    """Persist relay_*/relay_overrides to disk. Best-effort — never raises."""
    try:
        RELAY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RELAY_STATE_FILE.write_text(json.dumps({
            "default_enabled": relay_default_enabled,
            "overrides_enabled": {str(k): bool(v) for k, v in relay_overrides.items()},
            "default_target": relay_default_target,
            "overrides_target": {str(k): v for k, v in relay_targets.items()},
        }, indent=2))
    except Exception as e:
        print(f"[relay-state] failed to save {RELAY_STATE_FILE}: {e}", file=sys.stderr)


load_relay_state()


def load_pane_names() -> None:
    """Load pane_names_by_session from disk; safe on first run."""
    global pane_names_by_session
    try:
        with open(PANE_NAMES_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            pane_names_by_session = {
                str(k): str(v) for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str) and v
            }
    except FileNotFoundError:
        pane_names_by_session = {}
    except Exception as e:
        print(f"[pane-names] failed to load {PANE_NAMES_FILE}: {e}", file=sys.stderr)
        pane_names_by_session = {}


def save_pane_names() -> None:
    """Persist pane_names_by_session. Best-effort — never raises."""
    try:
        PANE_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        PANE_NAMES_FILE.write_text(json.dumps(pane_names_by_session, indent=2))
    except Exception as e:
        print(f"[pane-names] failed to save {PANE_NAMES_FILE}: {e}", file=sys.stderr)


load_pane_names()


_BRIDGE_CHROME_HOST_CACHE = {"host": None, "ts": 0.0}

def _candidate_bridge_chrome_hosts() -> list[str]:
    """Plausible hosts where Chrome --remote-debugging-port=9222 listens.
    CDP_HOST env var overrides; otherwise try localhost + WSL gateway
    (from /proc/net/route's default route) + nameserver from
    /etc/resolv.conf, deduplicated. Mirrors daemon/cdp_daemon.py."""
    out = []
    h = os.environ.get("CDP_HOST", "").strip()
    if h:
        out.append(h)
    out.append("127.0.0.1")
    # WSL default-route gateway (Windows side, NAT'd interface)
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    hex_gw = parts[2]
                    octs = [int(hex_gw[i:i+2], 16) for i in range(6, -2, -2)]
                    out.append(".".join(map(str, octs)))
                    break
    except Exception:
        pass
    # nameserver fallback
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        out.append(parts[1])
                    break
    except Exception:
        pass
    seen = set(); deduped = []
    for h in out:
        if h and h not in seen:
            seen.add(h); deduped.append(h)
    return deduped

def _resolve_bridge_chrome_host(timeout: float = 0.5) -> str:
    """Probe candidates and return the first that answers /json/version.
    Cached for 30s so we don't re-probe on every request."""
    now = time.time()
    if _BRIDGE_CHROME_HOST_CACHE["host"] and (now - _BRIDGE_CHROME_HOST_CACHE["ts"]) < 30:
        return _BRIDGE_CHROME_HOST_CACHE["host"]
    port = int(os.environ.get("CDP_PORT", "9222"))
    for host in _candidate_bridge_chrome_hosts():
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=timeout) as r:
                if r.status == 200:
                    _BRIDGE_CHROME_HOST_CACHE["host"] = host
                    _BRIDGE_CHROME_HOST_CACHE["ts"] = now
                    return host
        except Exception:
            continue
    return "127.0.0.1"  # fall through; caller's request will fail loudly


# 2026-08-30: /brN used to be assigned by enumerate() over the panel list, so
# it was a POSITION, not an identity. Opening, closing or renaming any panel
# renumbered every other one. A caller addressing /br1 could reach a different
# panel than it did a minute earlier, and an agent told "you are /br0" kept
# announcing that label after the server had started calling it /br1 — which is
# exactly how a reply came back tagged "/br0 → %2" from the panel addressed as
# /br1. The number is now bound to a tab_id for as long as the server lives, so
# an alias always names the same panel and freed numbers are never reused.
_short_by_tab: dict = {}
_SHORTS_LOADED = False
_short_next = [0]


def _load_shorts() -> None:
    """Restore tab -> /brN from disk so aliases survive a server restart."""
    try:
        data = json.loads(SHORTS_FILE.read_text())
        for k, v in (data.get("map") or {}).items():
            _short_by_tab[int(k)] = int(v)
        _short_next[0] = max([int(v) for v in _short_by_tab.values()] + [-1]) + 1
    except Exception:
        pass


def _save_shorts() -> None:
    try:
        SHORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHORTS_FILE.write_text(json.dumps(
            {"map": {str(k): v for k, v in _short_by_tab.items()}}), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] could not persist panel shorts: {e}", file=sys.stderr)


def _stable_short(tab_id) -> int:
    global _SHORTS_LOADED
    if not _SHORTS_LOADED:
        _SHORTS_LOADED = True
        _load_shorts()
    key = int(tab_id) if tab_id is not None else -1
    if key not in _short_by_tab:
        _short_by_tab[key] = _short_next[0]
        _short_next[0] += 1
        _save_shorts()
    return _short_by_tab[key]


def assign_short_names() -> list[dict]:
    """Build the unified panel list with stable /brN short aliases.

    Order: registered panels first (alpha by name), then discovered groups
    (alpha by group title). Discovered entries that match a registered panel's
    tab_id are dropped (the registered name wins).

    Each entry carries:
      - `short`        = "/brN"  — the canonical short alias for browser panels
      - `short_legacy` = "%N"    — back-compat alias kept for one transition
                                   release so existing CLI callers that read
                                   the JSON keep working. `%N` is otherwise
                                   reserved for tmux's own pane ids.
    """
    seen_tab_ids = set()
    unified: list[dict] = []
    # Registered panels store their name as the panels.json key, captured
    # when they were registered. Rename the tab group in Chrome (or close
    # and re-open it) and that key goes stale, so the dashboard kept showing
    # the old name while the extension popup — which merges against live
    # chrome.tabGroups — showed the new one. Prefer the live group title,
    # keyed by tab_id, and keep the stored key as `registered_name` so
    # rename/lookup by the panels.json key still works.
    live_title_by_tab = {
        d.get("tab_id"): (d.get("group_title") or t)
        for t, d in discovered_panels.items()
        if isinstance(d, dict) and d.get("tab_id") is not None
    }
    for name in sorted(panels.keys()):
        p = panels[name]
        tid = p.get("tab_id")
        seen_tab_ids.add(tid)
        unified.append({
            "kind": "registered",
            "name": live_title_by_tab.get(tid) or name,
            "registered_name": name,
            "to": p.get("to"),
            "tab_id": tid,
        })
    for title in sorted(discovered_panels.keys()):
        d = discovered_panels[title]
        if d.get("tab_id") in seen_tab_ids:
            continue
        unified.append({
            "kind": "discovered",
            "name": d.get("group_title") or title,
            "to": "browser",  # default agent for routing if user registers
            "tab_id": d.get("tab_id"),
            "tab_title": d.get("tab_title", ""),
        })
    for entry in unified:
        n = _stable_short(entry.get("tab_id"))
        entry["short"] = f"/br{n}"
        entry["short_legacy"] = f"%{n}"
    return unified


# Status-marker prefixes the extension prepends to a tab-group title to show
# panel lifecycle state (⌛ = working, ✅ = done, 🔄 = resuming, ❌ = error).
# These drift between the *registered* name (captured at one instant) and the
# *live* title (updated continuously), so an exact name match spuriously fails
# even when the panel is clearly live. Normalizing them away makes a label like
# "🦅 #1" resolve to a panel registered as "⌛🦅 #1".
_PANEL_STATUS_PREFIXES = ("⌛", "✅", "\U0001f504", "❌", "\U0001f514")  # ⌛ ✅ 🔄 ❌ 🔔 (permission prompt)


def _normalize_panel_label(s: str) -> str:
    """Strip any leading run of status-marker prefixes + surrounding whitespace,
    lower-case, and collapse internal whitespace — for tolerant label matching.
    Only the known status markers are stripped; a legitimate leading emoji such
    as 🦅 is preserved so distinct panels never collide."""
    if not s:
        return ""
    t = s.strip()
    changed = True
    while changed:
        changed = False
        for pfx in _PANEL_STATUS_PREFIXES:
            if t.startswith(pfx):
                t = t[len(pfx):].lstrip()
                changed = True
    return " ".join(t.split()).lower()


# Live side panels straight from Chrome, independent of the extension push.
# The registry (panels.json + the extension's discovery push) can lag or be
# empty: the extension in one window may hold a stale token (its pushes
# 401), side panels are tab-scoped so switching tabs drops them from the
# push, and a brand-new panel greets the terminal through the CDP daemon
# before any push has named it. In every one of those cases the panel is
# open and reachable, only the registry does not know it. This asks Chrome.
_cdp_cache: dict = {"ts": 0.0, "ids": set()}


def _cdp_probe_sidepanels() -> tuple[bool, set]:
    """Ask Chrome directly for its open side panels: (probe_ok, tab_ids).

    The ok flag exists for the close watcher. A failed probe — Chrome not
    running, debug port not listening, request timed out — also produces an
    empty set, and reading that as "every panel closed" would fire a close
    notice into every bound pane at once.
    """
    targets = _cdp_list()
    if targets is None:
        return False, set()
    ids: set = set()
    for t in targets:
        m = re.search(r"sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
        if m:
            ids.add(int(m.group(1)))
    return True, ids


def _cdp_hostport() -> tuple[str, int]:
    port = 9334
    try:
        agents = json.loads((Path.home() / ".config" / "claude-web-bridge" / "agents.json").read_text())
        port = int(next(iter(agents.values())).get("port") or port)
    except Exception:
        pass
    gw = subprocess.run(["sh", "-c", "ip route show default | awk '{print $3}'"],
                        capture_output=True, text=True, timeout=2).stdout.strip() or "127.0.0.1"
    return gw, port


# The last /json/list read, for the event detectors: they reuse the close
# watcher's once-a-second read instead of asking Chrome again.
_cdp_last: dict = {"ts": 0.0, "targets": None, "host": None}


def _cdp_list() -> list | None:
    """The bridge Chrome's /json/list, or None if it cannot be read."""
    try:
        gw, port = _cdp_hostport()
        with urllib.request.urlopen(f"http://{gw}:{port}/json/list", timeout=2) as r:
            targets = json.loads(r.read().decode())
    except Exception:
        return None
    _cdp_last.update({"ts": time.time(), "targets": targets, "host": (gw, port)})
    return targets


def _cdp_browser_id() -> str | None:
    """This Chrome launch's browser target id (/json/version). It changes on
    every Chrome start, so it tells a reused tab id from the same panel."""
    try:
        gw, port = _cdp_hostport()
        with urllib.request.urlopen(f"http://{gw}:{port}/json/version", timeout=2) as r:
            ws = json.loads(r.read().decode()).get("webSocketDebuggerUrl") or ""
        return ws.rsplit("/", 1)[-1] or None
    except Exception:
        return None


def _cdp_eval(target: dict, js: str, host: tuple[str, int]):
    """Evaluate js in one CDP target and return its value (read-only use)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
    from cdp import CDPClient
    gw, port = host
    ws = re.sub(r"//(127\.0\.0\.1|localhost):\d+", f"//{gw}:{port}", target["webSocketDebuggerUrl"])
    c = CDPClient(ws)
    c.connect()
    try:
        # awaitPromise: the service-worker lookups are async (chrome.* APIs);
        # without it they returned an unresolved Promise, i.e. no target and
        # no Open button (live 2026-10-25).
        r = c.send_command("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")
    finally:
        c.close()


def cdp_sidepanel_tab_ids() -> set:
    now = time.time()
    if now - _cdp_cache["ts"] < 3.0:
        return _cdp_cache["ids"]
    ok, ids = _cdp_probe_sidepanels()
    if not ok:
        ids = set()
    _cdp_cache.update({"ts": now, "ids": ids})
    return ids


def resolve_panel_name(name: str) -> dict | None:
    """Resolve a panel by full name, /brN short alias, or legacy %N alias.

    %N is accepted as a transition-release back-compat alias for /brN so that
    existing CLI callers (and any scripts that captured a %N at registration
    time) keep working. New callers should prefer /brN.

    Resolution order (most precise first):
      1. exact match on name / short / legacy alias
      2. raw tab_id (numeric, 6+ digits) — robust when labels drift entirely
      3. status-prefix-normalized name match (⌛/✅/🔄/❌ tolerant), so a live
         title resolves to a registry entry captured with a different marker.
    """
    if not name:
        return None
    unified = assign_short_names()
    # 1. exact, also on the registered name: until a rename reaches Chrome the
    # live group title (e["name"]) is still "Claude", and a message sent right
    # after open-panel --name got "unknown panel" (2026-10-25).
    for e in unified:
        if (e["name"] == name or e.get("registered_name") == name
                or e["short"] == name or e.get("short_legacy") == name):
            return e
    # 2. raw tab_id
    if name.isdigit() and len(name) >= 6:
        tid = int(name)
        for e in unified:
            if e.get("tab_id") == tid:
                return e
        # 2b. Not in the registry, but Chrome has that side panel open right
        # now: dispatch to it anyway. See cdp_sidepanel_tab_ids for why the
        # registry can be behind a panel that is plainly alive.
        if tid in cdp_sidepanel_tab_ids():
            print(f"[INFO] resolve: tab {tid} not in registry, found live via CDP", file=sys.stderr)
            return {"name": f"tab:{tid}", "tab_id": tid, "to": "browser",
                    "short": f"tab:{tid}", "kind": "cdp-live"}
    # 3. normalized (status-prefix tolerant)
    want = _normalize_panel_label(name)
    if want:
        matches = [e for e in unified if want in (_normalize_panel_label(e["name"]),
                                                   _normalize_panel_label(e.get("registered_name") or ""))]
        if len(matches) == 1:
            return matches[0]
        # >1 normalized match → ambiguous; fall through to None rather than guess
    return None
AGENT_PORT_RANGE = (9222, 9240)  # inclusive — keep in sync with the relay/firewall


def next_free_port() -> int:
    """Pick the lowest port in AGENT_PORT_RANGE not already used by an agent."""
    used = {info.get("port") for info in agents.values() if info.get("port")}
    for p in range(AGENT_PORT_RANGE[0], AGENT_PORT_RANGE[1] + 1):
        if p not in used:
            return p
    raise RuntimeError(f"all ports in range {AGENT_PORT_RANGE} are in use")


def deliver_reply(from_pane: str, text: str, from_agent: str | None = None,
                  panel: str | None = None,
                  wait_ready: bool = False,
                  max_wait: float | None = None) -> tuple[bool, str]:
    """
    Deliver text into a tmux pane reliably for any size.

    For small text (<1500 chars), use tmux-bridge as before — preserves the
    existing read-guard semantics and the standard sender header.

    For large text, char-by-char send-keys is slow and unreliable: characters
    can be dropped, the receiving TUI may interpret partial input, and the
    Enter we fire afterward can race with the still-arriving paste. Switch
    to tmux's load-buffer + paste-buffer for atomic delivery, then submit
    after a settle delay proportional to length.

    Replies are not written to disk (removed 2026-10-25, patch note 21).

    `panel` (a /brN alias) switches to the single-header browser format:
        [browser-bridge /br2 → %1] <text>
    That path always uses paste-buffer, because tmux-bridge would prepend a
    second header of its own and the whole point is one header. It also names
    the panel that actually spoke instead of a Chrome instance name, which is
    identical across every panel and so identifies nothing.
    """
    env = os.environ.copy()
    # NOT env.get(...): the server usually inherits TMUX_PANE from whichever
    # pane launched it, and tmux-bridge then stamps every browser message as
    # coming from that pane. Messages from a browser panel were being labelled
    # as coming from the operator's own terminal.
    env["TMUX_PANE"] = "%bridge"

    if panel:
        # Greetings before 2026-09-19 told the panel to open its line with
        # this exact tag, and this branch prepends it too, so replies read
        # "[browser-bridge /br14 → %0] [browser-bridge /br14 → %0] OK ...".
        # The greeting no longer asks for it (Claude for Chrome refused that
        # instruction as injection), but a panel may still echo it from an
        # older conversation. Drop the panel's copy when it is there.
        tag = f"[browser-bridge {panel} → {from_pane}]"
        body = text.strip()
        while body.startswith(tag):
            body = body[len(tag):].strip()
        try:
            body, structured = structure_reply(body)
        except Exception:
            structured = False  # formatting must never block delivery
        sep = "\n" if structured else " "
        return _paste_into_pane(from_pane, f"{tag}{sep}{body}", wait_ready=wait_ready,
                                max_wait=max_wait)

    SMALL_THRESHOLD = 1500
    # For small messages we use tmux-bridge's `message` which prepends its own
    # sender header. Agent name is encoded by prefixing the text.
    body_for_small = (f"[agent:{from_agent}] " + text) if from_agent else text
    if len(text) < SMALL_THRESHOLD:
        try:
            subprocess.run([TMUX_BRIDGE, "read", from_pane, "5"],
                           check=True, capture_output=True, env=env, timeout=5)
            subprocess.run([TMUX_BRIDGE, "message", from_pane, body_for_small],
                           check=True, capture_output=True, env=env, timeout=10)
            subprocess.run([TMUX_BRIDGE, "read", from_pane, "5"],
                           check=True, capture_output=True, env=env, timeout=5)
            subprocess.run([TMUX_BRIDGE, "keys", from_pane, "Enter"],
                           check=True, capture_output=True, env=env, timeout=5)
            return True, "ok"
        except subprocess.CalledProcessError as e:
            return False, f"tmux-bridge failed: {e.stderr.decode(errors='replace')}"
        except FileNotFoundError:
            return False, f"tmux-bridge binary not found at {TMUX_BRIDGE}"
        except subprocess.TimeoutExpired:
            return False, "tmux-bridge timed out"

    # Large text: tmux paste-buffer for atomic delivery.
    sender_id = f"agent:{from_agent}" if from_agent else "%bridge"
    header = f"[tmux-bridge from:{sender_id} pane:%bridge at::.] "
    return _paste_into_pane(from_pane, header + text)


# How long a delivery waits for a busy pane before giving up and leaving the
# message unsubmitted.
PANE_WAIT_S = float(os.environ.get("CLAUDE_BRIDGE_PANE_WAIT_S", "4"))
# How long a reply waits for the operator's half-typed input (or bash mode) to
# clear before it is pasted as an unsubmitted draft (or, in bash mode, dropped).
REPLY_WAIT_S = float(os.environ.get("CLAUDE_BRIDGE_REPLY_WAIT_S", "120"))
# A heartbeat older than this is treated as stale, and ranking falls back
# to tmux activity so a long-idle Claude pane cannot hold the routing.
HEARTBEAT_FRESH_S = float(os.environ.get("CLAUDE_BRIDGE_HEARTBEAT_FRESH_S", "1800"))


# Greyed hint text Claude Code draws on an EMPTY prompt line. Matched as
# prefixes, lowercase. Anything here means the operator has typed nothing.
_PROMPT_HINTS = (
    "press up to edit",
    "try \"",
    "ask claude",
    "type a message",
    "run /",
    "\u2026",
)


def _pane_ready(pane: str) -> tuple[bool, str]:
    """Is this pane safe to paste into and submit?

    Two blockers, both visible in the pane's own rendering:
      * the agent is mid-turn — Claude Code shows "esc to interrupt", and the
        Enter we send would cancel that turn rather than submit our message
      * the operator has text typed but not sent — our paste would append to
        their line and the Enter would send the mixture

    Returns (ready, reason). Unreadable pane counts as ready: refusing to
    deliver because a capture failed would be worse than delivering.
    """
    try:
        res = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane],
                             capture_output=True, text=True, timeout=3)
        if res.returncode != 0:
            return True, "capture failed"
        lines = [ln.rstrip() for ln in (res.stdout or "").splitlines()]
    except Exception:
        return True, "capture error"

    tail = "\n".join(lines[-14:]).lower()
    # 2026-09-19: a mid-turn agent is NOT a blocker any more. Claude Code
    # queues text submitted during a turn and surfaces it as "The user sent a
    # new message while you were working"; it does not interrupt. Verified
    # today: three bridge error relays landed mid-turn that way. Gating on
    # "esc to interrupt" left a panel's READY reply pasted but unsubmitted in
    # the operator's pane, and he had to press Enter himself. The only thing
    # that still blocks is half-typed operator input, checked below.

    # Draft text: the last prompt line carrying more than the marker itself.
    # Claude Code renders greyed placeholder hints on that same line when it is
    # empty. Reading those as typed input made every announcement a draft.
    # Claude Code separates the marker from the text with U+00A0, not a plain
    # space, so normalise before matching or the check never fires.
    for ln in reversed(lines[-14:]):
        st = ln.replace("\u00a0", " ").strip()
        # 2026-10-04: Claude Code's bash mode shows "!" where the prompt marker
        # is. A reply pasted and submitted there ran as a shell command (three
        # did, harmlessly). Never deliver into bash mode, even with no text.
        if st[:1] == "!" and (len(st) == 1 or st[1] in (" ", "\u00a0")):
            return False, "prompt is in bash mode"
        if st[:1] in ("❯", ">"):
            rest = st[1:].strip()
            low = rest.lower()
            if rest and not any(low.startswith(h) for h in _PROMPT_HINTS):
                return False, "operator has unsent input"
            break
    return True, "ready"


def _draft_probes(full_text: str) -> list[str]:
    """Distinctive lines of a pasted message, to spot it still sitting unsent."""
    return [ln.strip()[:40] for ln in full_text.splitlines() if len(ln.strip()) >= 6][:4]


def _draft_in_prompt(from_pane: str, probes: list[str]) -> bool:
    """True when our message is still typed into the pane's live input box.

    The box is the LAST "❯" line plus the lines under it up to the box's
    bottom rule. Claude Code echoes a queued message on an earlier "❯" line
    once Enter was accepted mid-turn, so only the last one counts.

    2026-10-04: the old check looked for the header on the "❯" line only. A
    multi-line paste does not show its first line there (verified: the prompt
    read "❯ Answer" with the "[browser-bridge /brN → %N]" header elsewhere), so
    every multi-line reply looked submitted, no Enter was retried, and two
    replies sat in the user's prompt until they pressed Enter themselves."""
    try:
        cap = subprocess.run(["tmux", "capture-pane", "-p", "-t", from_pane],
                             capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    lines = [ln.replace("\u00a0", " ").strip() for ln in cap.splitlines()]
    prompt_idx = [i for i, ln in enumerate(lines) if ln[:1] in ("\u276f", ">")]
    if not prompt_idx:
        return False
    box = []
    for ln in lines[prompt_idx[-1]:]:
        if ln.startswith("\u2500"):  # the box's bottom rule
            break
        box.append(ln)
    text = "\n".join(box)
    # A bracketed paste shows as a placeholder, not as its text.
    return "[Pasted text #" in text or any(p in text for p in probes if p)


def _keep_submitting(from_pane: str, marker: list[str],
                     every_s: float = 2.0, give_up_s: float = 60.0) -> None:
    end = time.monotonic() + give_up_s
    presses = 0
    while time.monotonic() < end:
        time.sleep(every_s)
        if not _draft_in_prompt(from_pane, marker):
            print(f"[INFO] reply into {from_pane} submitted after {presses} extra Enter(s)", flush=True)
            return
        subprocess.run(["tmux", "send-keys", "-t", from_pane, "Enter"],
                       check=False, capture_output=True, timeout=5)
        presses += 1
    print(f"[WARN] reply into {from_pane} still a draft after {give_up_s:.0f}s", flush=True)


def _paste_into_pane(from_pane: str, full_text: str,
                     wait_ready: bool = False,
                     max_wait: float | None = None) -> tuple[bool, str]:
    """
    Put an already-headed message into a pane atomically via tmux buffers.

    Used for anything too long for send-keys, and for every browser-format
    message regardless of length, since tmux-bridge would add a header of its
    own on top of the one already built.
    """
    env = os.environ.copy()
    env["TMUX_PANE"] = "%bridge"
    payload = full_text.encode("utf-8")

    # Wait for the pane to be safe to type into. Pasting into a pane whose
    # agent is mid-turn makes the trailing Enter an interrupt, and pasting on
    # top of half-typed operator input mangles it. Both were observed: a panel
    # announcement landed in a pane the operator was using and produced
    # "Interrupted - What should Claude do instead?".
    # Only unsolicited traffic waits. A reply the operator is actively waiting
    # on must go in immediately: gating it blocked the HTTP handler for 20s,
    # the caller timed out, and writing the response then raised BrokenPipeError.
    ready, why = True, "not gated"
    if wait_ready:
        waited = 0.0
        ready, why = _pane_ready(from_pane)
        budget = PANE_WAIT_S if max_wait is None else max_wait
        while not ready and waited < budget:
            time.sleep(1.0)
            waited += 1.0
            ready, why = _pane_ready(from_pane)
        if not ready and why == "prompt is in bash mode":
            # Never paste into bash mode: even unsubmitted, the operator's next
            # Enter would run it. Replies are not kept on disk, so it is dropped.
            return False, f"not pasted: {from_pane} stayed in bash mode for {waited:.0f}s; reply dropped"
    try:
        # Optional read-guard satisfaction so subsequent tmux-bridge ops still work.
        try:
            subprocess.run([TMUX_BRIDGE, "read", from_pane, "5"],
                           check=False, capture_output=True, env=env, timeout=5)
        except Exception:
            pass
        subprocess.run(["tmux", "load-buffer", "-"], input=payload,
                       check=True, capture_output=True, timeout=10)
        # -p: bracketed paste, so the TUI knows exactly where the paste starts
        # and ends and never reads a newline inside it as Enter. Claude Code
        # then shows it as "[Pasted text #N +M lines]" (see _draft_in_prompt).
        subprocess.run(["tmux", "paste-buffer", "-p", "-t", from_pane],
                       check=True, capture_output=True, timeout=10)
        # Settle delay scales with payload size — longer pastes need more time
        # for the receiving TUI to ingest before we send Enter.
        settle = min(5.0, 0.5 + len(payload) / 4000.0)
        if b"\n" in payload:
            # A multi-line paste takes the TUI longer to ingest, most of all
            # while its agent is mid-turn. An Enter sent too early is dropped.
            settle = max(settle, 1.2)
        time.sleep(settle)
        if ready:
            subprocess.run(["tmux", "send-keys", "-t", from_pane, "Enter"],
                           check=True, capture_output=True, timeout=5)
            submitted = "submitted"
            # 2026-09-19: verify the submit took. A panel's READY reply was
            # pasted into the operator's pane and the Enter was not accepted
            # (multi-line body, TUI still ingesting), so it sat as a draft
            # until he pressed Enter himself. Check the prompt line; if our
            # header is still sitting there, press Enter once more.
            marker = _draft_probes(full_text)
            for attempt in (1, 2):
                time.sleep(0.9)
                if not _draft_in_prompt(from_pane, marker):
                    break
                subprocess.run(["tmux", "send-keys", "-t", from_pane, "Enter"],
                               check=False, capture_output=True, timeout=5)
                submitted = f"submitted (Enter re-sent x{attempt})"
            else:
                # 2026-09-29: a busy pane (agent mid-turn) can swallow several
                # Enters in a row and take the next one many seconds later. The
                # HTTP handler must not wait that long, so keep pressing Enter
                # from a background thread until the draft is gone.
                threading.Thread(target=_keep_submitting,
                                 args=(from_pane, marker), daemon=True).start()
                submitted = "Enter not accepted yet, retrying in background"
        else:
            # Left unsubmitted on purpose: the operator presses Enter when
            # they are ready. Losing the auto-submit beats interrupting them.
            submitted = f"left as draft ({why})"
        return True, (f"ok (paste-buffer, {len(payload)}B, settle={settle:.1f}s, "
                      f"{submitted})")
    except subprocess.CalledProcessError as e:
        return False, f"tmux paste failed: {e.stderr.decode(errors='replace') if e.stderr else e}"
    except FileNotFoundError:
        return False, "tmux binary not found"
    except subprocess.TimeoutExpired:
        return False, "tmux paste timed out"


def pick_last_active_pane() -> str | None:
    """
    The tmux pane whose agent most recently took a prompt from the operator.

    Two signals, and a pane is ranked by whichever of its own is newer.

    1. POST /tmux-pane/heartbeat, fired by a Claude Code UserPromptSubmit hook.
       Precise, because it means the operator actually submitted a prompt. Only
       Claude Code agents have it.
    2. tmux #{window_activity}. Vendor-neutral, so a Codex or any other agent is
       visible too, but it tracks OUTPUT. A pane that talks without being asked
       (a long build, a chatty background job) can outrank the pane the operator
       really used. That is the known cost of covering non-Claude agents.

    Ranking on the max of the two rather than preferring one keeps the precise
    signal where it exists and falls back to the universal one where it does
    not, with no special cases. Heartbeat-only ranking silently pinned every
    confirmation to the Claude panes, since a Codex pane could never score.

    Returns None when nothing is known, or when every candidate is no longer a
    live pane (closed terminal, restarted tmux server).
    """
    with lock:
        beats = dict(last_prompt_sent_per_pane)
        pinned = announce_pin
        pinned_at = announce_pin_at

    activity: dict[str, float] = {}
    live: set[str] = set()
    try:
        res = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{window_activity}"],
            capture_output=True, text=True, timeout=3)
        # A pin only counts while its pane is still alive; a dead pin would
        # silently swallow every announcement. It also only counts until the
        # operator prompts a DIFFERENT pane after pinning: tmux recycles pane
        # ids, so a pin left over from an earlier day (%1 then, %1 now, a
        # different agent) hijacked announcements from the agent actually
        # being talked to. A heartbeat newer than the pin is the operator
        # moving on, and that pane wins.
        live_ids = {ln.split()[0] for ln in (res.stdout or "").splitlines() if ln.split()}
        if pinned and pinned in live_ids:
            newer = {p: ts for p, ts in beats.items()
                     if p in live_ids and p != pinned and ts > pinned_at}
            if not newer:
                return pinned
            print(f"[INFO] announce pin {pinned} (set {int(time.time() - pinned_at)}s ago) "
                  f"overridden by newer heartbeat from {max(newer, key=newer.get)}",
                  file=sys.stderr)
        for ln in (res.stdout or "").splitlines():
            parts = ln.split()
            if not parts:
                continue
            live.add(parts[0])
            if len(parts) > 1:
                try:
                    activity[parts[0]] = float(parts[1])
                except ValueError:
                    pass
    except Exception:
        pass

    scores: dict[str, float] = {}
    for pane_id in set(beats) | set(activity):
        if live and pane_id not in live:
            continue
        scores[pane_id] = max(beats.get(pane_id, 0.0), activity.get(pane_id, 0.0))
    if not scores:
        return None

    # Heartbeats win outright when any are fresh. Ranking on max(heartbeat,
    # activity) let a pane that merely PRODUCES output outrank the pane the
    # operator actually prompted: a long tool run keeps window_activity at
    # "now" forever, so announcements followed whichever agent was busiest
    # rather than whichever one was being talked to. Activity stays as the
    # fallback so non-Claude agents, which never heartbeat, remain reachable.
    now = time.time()
    fresh = {pane: ts for pane, ts in beats.items()
             if pane in live and (now - ts) <= HEARTBEAT_FRESH_S}
    if fresh:
        return max(fresh.items(), key=lambda kv: kv[1])[0]
    return max(scores.items(), key=lambda kv: kv[1])[0]


def short_for_tab(tab_id: int | None) -> str:
    """
    The /brN alias for a tab, which is what the operator actually types at a
    target. Falls back to the raw tab id when the alias table has not caught up
    with this push yet, so a message is never dropped for want of a nice name.
    """
    if tab_id is None:
        return "browser"
    try:
        for entry in assign_short_names():
            if entry.get("tab_id") == int(tab_id) and entry.get("short"):
                return entry["short"]
    except Exception:
        pass
    return f"tab:{tab_id}"


def announce_new_panel(tab_id: int, panel_name: str) -> None:
    """
    Tell the operator's most recent terminal agent that a browser panel opened.

    Runs off the request thread: deliver_reply shells out to tmux-bridge four
    times and can take seconds, and the extension's discovery push must not
    block on that.
    """
    pane = pick_last_active_pane()
    if not pane:
        print("[INFO] new panel but no heartbeated pane to announce to",
              file=sys.stderr)
        return
    short = short_for_tab(tab_id)
    # A panel outside the chrome-debug profile has no CDP target, so the
    # daemon cannot type into it. Stay silent in the pane and log it.
    #
    # 2026-09-20: this branch used to deliver one
    #   "unreachable: this panel is not in the chrome-debug Chrome, so the
    #    bridge cannot drive it"
    # line into the pane, once per tab id. The user asked for it gone: it arrives
    # unprompted in the middle of unrelated work, and it is often simply
    # wrong, because a panel that is still opening has no CDP target yet and
    # trips this check a second before it becomes drivable. The operator
    # already finds out when a dispatch to that panel fails, which is the
    # moment the information matters. `unreachable_noted` is kept so the log
    # line stays once per tab. To restore the notice, put the `if first:`
    # deliver_reply block back — see patch note 13.
    if int(tab_id) not in cdp_sidepanel_tab_ids():
        with lock:
            first = int(tab_id) not in unreachable_noted
            unreachable_noted.add(int(tab_id))
        print(f"[INFO] panel {panel_name}(tab_id={tab_id}) has no side panel in the "
              f"chrome-debug instance; not announced"
              f"{'' if first else ' (already noted)'}", file=sys.stderr)
        return
    ok, detail = deliver_reply(pane, "connected", panel=short, wait_ready=True)
    # Always log detail, not just ok/fail: detail is what says whether the
    # message was submitted or left as a draft because the pane was busy.
    print(f"[INFO] announced panel {panel_name}(tab_id={tab_id}) -> {pane}: "
          f"{detail}", file=sys.stderr)
    # 2026-10-04: no greeting into the panel any more. claude.ai titles a chat
    # from its first message, so every greeted panel became a "Connection
    # test" chat even when real work followed (18 of them in Recent tasks).
    # The first message is now the caller's task. greet_panel() is kept: call
    # it here again to restore the handshake.


def note_task_send(task_id: str, mid: str, pane: str, panel: str, data: dict) -> None:
    """Record one task message. Caller holds `lock`. The same step sent again
    is a retry (attempt + 1); a new step starts at attempt 1."""
    now = time.time()
    step, total = None, None
    raw = str(data.get("step") or "").strip()
    if raw:
        a, _, b = raw.partition("/")
        try:
            step = int(a)
            total = int(b) if b else None
        except ValueError:
            step = None
    rec = tasks.get(task_id)
    if rec is None:
        rec = tasks[task_id] = {"task_id": task_id, "started_at": now, "attempt": 0}
    same_step = step is not None and rec.get("step") == step and rec["attempt"] > 0
    rec["attempt"] = rec["attempt"] + 1 if same_step else 1
    if not same_step:
        rec["step_started_at"] = now
    if step is not None:
        rec["step"] = step
    if total is not None:
        rec["steps_total"] = total
    if data.get("step_name"):
        rec["step_name"] = str(data["step_name"])[:120]
    rec.update({"pane": pane, "panel": panel, "state": "working", "ended_at": None,
                "actions": 0, "last_action": "", "dispatch_id": mid})
    rec.setdefault("step_started_at", now)
    task_by_dispatch[mid] = task_id


def note_task_reply(mid: str, is_error: bool) -> None:
    """A reply ends the current message. A reply to the last planned step ends
    the task. Caller holds `lock`."""
    rec = tasks.get(task_by_dispatch.pop(mid, ""))
    if not rec or rec.get("dispatch_id") != mid:
        return
    if is_error:
        rec["state"], rec["ended_at"] = "failed", time.time()
    elif rec.get("steps_total") and rec.get("step") == rec["steps_total"]:
        rec["state"], rec["ended_at"] = "done", time.time()
    else:
        rec["state"] = "working"  # between steps; the next send moves it on


def greet_panel(tab_id: int, short: str, pane: str) -> None:
    """
    Tell the browser panel it is on the bridge, and who it is bound to.

    A handshake is two-sided. Without this the panel has no idea it has a
    terminal counterpart or which pane that is, and it finds out only when a
    task lands on it unannounced.

    Submitted rather than left as a draft: an unread greeting is not a
    handshake. It costs one short turn of that panel's context per connect,
    which is the price of knowing the link actually works.
    """
    # Written the way the user would type it. Claude for Chrome treats its composer
    # as the human's turn and judges the text by shape: a bracketed tag, "You
    # are connected", a reply format it must follow, all read as machine text
    # planted by the page, and the panel refused the whole greeting as a prompt
    # injection (2026-09-19, panel open on platform.claude.com). No tag is
    # needed: deliver_reply stamps every reply with the panel's tag itself.
    text = (f"Hi. I'm messaging you from my terminal, pane {pane}. "
            f"I'll refer to this chat as {short}. Reply with a short ok so I "
            f"know the link works.")
    with lock:
        item = {
            "id": secrets.token_hex(8),
            "from_pane": pane,
            "to_panel": short,
            "to_agent": "browser",
            "tab_id": int(tab_id),
            "mode": "sidepanel",
            "text": text,
            "auto_submit": True,
            "keys_only": False,
            "enqueued_at": time.time(),
        }
        outbox.append(item)
        in_flight[item["id"]] = {
            "from_pane": pane,
            "to_agent": item["to_agent"],
            "tab_id": int(tab_id),
        }
    print(f"[INFO] greeted panel {short}(tab_id={tab_id}) bound to {pane}",
          file=sys.stderr)


def announce_closed_panel(tab_id: int, short: str, pane: str) -> None:
    """One line into the pane that owned the panel. Nothing else."""
    ok, detail = deliver_reply(pane, "closed", panel=short, wait_ready=True)
    print(f"[INFO] panel {short}(tab_id={tab_id}) closed -> {pane}: {detail}",
          file=sys.stderr)


def panel_close_watcher() -> None:
    """Notify the bound pane when a side panel it owns disappears.

    Chrome is the only authority here, never the extension push. A push is one
    profile's partial view and tabs flip in and out of it every few seconds;
    trusting an absence in a push is exactly what produced the announce loop
    in patch note 10. So this watcher reads `_cdp_probe_sidepanels()` only,
    skips probes that failed outright, and requires CLOSE_CONFIRM_POLLS
    consecutive successful probes without the tab before it says anything.
    """
    while True:
        time.sleep(CLOSE_POLL_S)
        try:
            ok, ids = _cdp_probe_sidepanels()
            if not ok:
                continue
            # Alias lookup walks the registry, which callers may hold `lock`
            # for. Resolve before taking it.
            shorts = {tid: short_for_tab(tid) for tid in ids}
            notices = []
            with lock:
                for tid in ids:
                    panel_alive_once.add(tid)
                    panel_absent_polls.pop(tid, None)
                    # Same tab id open again: it is a live panel once more, so
                    # a future close is allowed to report itself.
                    close_notified.discard(tid)
                    if shorts.get(tid):
                        panel_short_seen[tid] = shorts[tid]
                for tid in sorted(panel_alive_once - ids):
                    n = panel_absent_polls.get(tid, 0) + 1
                    panel_absent_polls[tid] = n
                    if n < CLOSE_CONFIRM_POLLS or tid in close_notified:
                        continue
                    close_notified.add(tid)
                    panel_alive_once.discard(tid)
                    panel_absent_polls.pop(tid, None)
                    if tid in closing_intended:
                        closing_intended.discard(tid)
                        panel_short_seen.pop(tid, None)
                        continue
                    # The owning pane only. No broadcast, no last-active-pane
                    # heuristic: a close notice in the wrong terminal is worse
                    # than none.
                    pane = relay_targets.get(tid) or relay_default_target
                    notices.append((tid, panel_short_seen.pop(tid, f"tab:{tid}"), pane))
            for tid, short, pane in notices:
                if not pane:
                    print(f"[INFO] panel tab_id={tid} closed but no relay target to notify",
                          file=sys.stderr)
                    continue
                threading.Thread(target=announce_closed_panel,
                                 args=(tid, short, pane), daemon=True).start()
        except Exception as e:
            print(f"[WARN] panel close watcher: {e}", file=sys.stderr)


NOTIFY_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge-notify"
# Detector event -> the allowlisted popup kind (~/.config/claude-web-bridge/notify.json).
EVENT_KINDS = {"prompt": "waiting-for-ok", "signed_out": "extension-signed-out",
               "login": "login-needed", "stopped": "stopped-irreversible"}


def ttl_text(seconds: float | None = None) -> str:
    """The panel time limit in words: "5 h", "2 min", "90 s"."""
    s = PANEL_TTL_S if seconds is None else seconds
    if s >= 3600:
        return f"{round(s / 3600, 1):g} h"
    if s >= 60 and s % 60 == 0:
        return f"{s / 60:g} min"
    return f"{s:g} s"


def notify_user(text: str, tab_id: int | str | None = None, sticky: bool = False,
                key: str | None = None, kind: str = "") -> None:
    """A popup on the user's screen through bridge-notify (plan A1). bridge-notify
    refuses kinds the user has not allowed (exit 4). tab_id is a tab id or a CDP
    page target id; either gives the popup an Open button to that page."""
    if not kind or not NOTIFY_SCRIPT.exists():
        print(f"[NOTIFY] {kind or '?'}: {text}", file=sys.stderr)
        return
    cmd = [str(NOTIFY_SCRIPT), "--kind", kind, "--text", text]
    if tab_id is not None:
        cmd += ["--tab", str(tab_id)]
    if sticky:
        cmd.append("--sticky")
    if key:
        cmd += ["--key", key]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except Exception as e:
        print(f"[WARN] bridge-notify failed: {e}", file=sys.stderr)


def _panel_busy(tab_id: int) -> bool:
    """A dispatch to the tab is queued or waiting for its reply. Caller holds `lock`."""
    return (any(isinstance(v, dict) and v.get("tab_id") == tab_id for v in in_flight.values())
            or any(it.get("tab_id") == tab_id and it.get("mode") != "control" for it in outbox))


def _group_title(tab_id: int) -> str:
    """The tab's group title from the last extension push. Caller holds `lock`."""
    for v in discovered_panels.values():
        if v.get("tab_id") == tab_id:
            return v.get("group_title") or ""
    return ""


def _known_host_tabs(cdp_live: set) -> dict:
    """Tab ids known to host a panel: live side panels, panel-meta and
    panels.json, plus panel-meta's group id -> tab id. Caller holds `lock`."""
    tabs = set(cdp_live) | {p.get("tab_id") for p in panels.values()}
    groups = {}
    for e in panel_meta_store.entries():
        if not e.get("orphan_since"):
            tabs.add(e["tab_id"])
            if e.get("group_id") is not None:
                groups[e["group_id"]] = e["tab_id"]
    return {"tabs": tabs, "groups": groups}


def _host_tab(item: dict, known: dict) -> tuple[int, str | None]:
    """(host tab id, its CDP target id) for one pushed group. The extension
    reports the group's first tab, which is a task tab once the agent opens a
    tab before its panel's tab (A3F4). Prefer a tab known to host a panel,
    then the group a named panel was seen in, then the first tab."""
    first = int(item["tab_id"])
    cands = []
    for t in item.get("tab_ids") or []:
        try:
            cands.append(int(t))
        except (TypeError, ValueError):
            pass
    host = next((t for t in cands or [first] if t in known["tabs"]), None)
    if host is None:
        host = known["groups"].get(item.get("group_id"), first)
    targets = item.get("target_ids") or {}
    target = targets.get(str(host)) or (item.get("target_id") if host == first else None)
    return host, target or None


def _group_target(tab_id: int) -> str | None:
    """The host tab's CDP target id from the last extension push. Caller holds `lock`."""
    for v in discovered_panels.values():
        if v.get("tab_id") == tab_id:
            return v.get("target_id")
    return None


def _drop_registered(tab_id: int) -> None:
    """Remove the tab's panels.json entry. Caller holds `lock`."""
    for n in [n for n, p in panels.items() if p.get("tab_id") == tab_id]:
        panels.pop(n)
        save_panels(panels)


def _sweep_delete(entry: dict) -> None:
    """Delete the chat and close the group. bridge-close-panel re-checks the
    stop button right before Delete, so a reply that started since the
    decision still blocks it. --no-raise: exit 3 instead of taking focus."""
    tid, name = entry["tab_id"], entry["name"]
    cmd = [str(CLOSE_PANEL_SCRIPT), str(tid), "--delete", "--no-raise"]
    if entry.get("chat_id"):
        cmd += ["--expect-chat", entry["chat_id"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        rc, out = r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        rc, out = -1, str(e)
    finally:
        with lock:
            cleaning_tabs.discard(tid)
    if rc == 0:
        with lock:
            panel_meta_store.pop(tid)
            _drop_registered(tid)
        print(f"[INFO] sweep: deleted {name} (tab {tid}) after {ttl_text()}: {out}", file=sys.stderr)
        return
    with lock:
        n = int(entry.get("deferrals", 0)) + 1
        panel_meta_store.update(tid, deferrals=n)
    print(f"[INFO] sweep: {name} (tab {tid}) not deleted, try {n} (rc={rc}): {out}", file=sys.stderr)
    if n == SWEEP_MAX_DEFERRALS:
        with lock:
            page = _group_target(tid)
        notify_user(f"Could not clean up {name}.", tab_id=page or _tab_target(tid) or tid,
                    key=f"sweep-{tid}", kind="cleanup-failed")


def panel_sweep_once(now: float) -> None:
    """One pass of the 5 h cleanup. Decides nothing while Chrome cannot be
    read: an empty list would make every panel look like an orphan."""
    targets = _cdp_list()
    if targets is None:
        return
    live = set()
    for t in targets:
        m = re.search(r"sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
        if m:
            live.add(int(m.group(1)))
    chats = panel_meta.chat_ids(targets, CFC_EXT_ID)
    ptargets = panel_meta.panel_targets(targets, CFC_EXT_ID)
    browser = _cdp_browser_id()
    if browser is None:
        return  # cannot tell this Chrome launch from the one the panels were opened in
    notify, delete = [], []
    with lock:
        for e in panel_meta_store.entries():
            tid = e["tab_id"]
            if not e.get("browser_id"):
                # Opened while Chrome's id could not be read: adopt this
                # launch, decide next sweep.
                panel_meta_store.update(tid, browser_id=browser)
                continue
            live_target = _group_target(tid)
            # Record identity the first time it is seen, never overwrite it,
            # and only from a read that is provably this panel's Chrome
            # launch and, once known, its own side panel (A3F3).
            if (e["browser_id"] == browser and not e.get("orphan_since")
                    and (not e.get("panel_target") or e["panel_target"] == ptargets.get(tid))):
                first = {}
                if ptargets.get(tid) and not e.get("panel_target"):
                    first["panel_target"] = ptargets[tid]
                if chats.get(tid) and not e.get("chat_id"):
                    first["chat_id"] = chats[tid]
                if first:
                    panel_meta_store.update(tid, **first)
                    e = panel_meta_store.get(tid)
            prefix, _ = panel_meta.split_prefix(_group_title(tid))
            if prompt_dom.get(tid):
                prefix = panel_meta.PROMPT   # the frame shows a permission prompt (title may say ✅)
            act = panel_meta.decide(e, now, ttl=PANEL_TTL_S, tab_live=tid in live,
                                    title_prefix=prefix, busy=_panel_busy(tid),
                                    live_chat=chats.get(tid), live_target=live_target,
                                    live_browser=browser, live_panel_target=ptargets.get(tid))
            if act == panel_meta.ORPHAN:
                if not e.get("orphan_since"):
                    panel_meta_store.update(tid, orphan_since=now)
                    print(f"[INFO] sweep: {e['name']} (tab {tid}) is no longer its panel; "
                          f"chat {e.get('chat_id') or 'unknown'} kept, see GET /panels/meta",
                          file=sys.stderr)
                continue
            if e.get("orphan_since"):
                panel_meta_store.update(tid, orphan_since=None)
            if act in (panel_meta.BUSY, panel_meta.HOLD_PROMPT) and sweep_said.get(tid) != act:
                print(f"[INFO] sweep: {e['name']} (tab {tid}) past {ttl_text()}, kept: "
                      f"{'replying' if act == panel_meta.BUSY else 'waiting for an OK'}", file=sys.stderr)
            sweep_said[tid] = act
            if act == panel_meta.NOTIFY_PROMPT:
                panel_meta_store.update(tid, prompt_notified_at=now)
                notify.append(dict(e))
            elif act == panel_meta.DELETE:
                cleaning_tabs.add(tid)  # /poll hands it nothing until done (A3F2)
                delete.append(dict(e))
    for e in notify:
        with lock:
            page = _group_target(e["tab_id"])
        notify_user(f"{e['name']} is past {ttl_text()} and waiting for your OK.",
                    tab_id=page or _tab_target(e["tab_id"]) or e["tab_id"], sticky=True, key=f"ttl-prompt-{e['tab_id']}", kind="waiting-for-ok")
    for e in delete:
        _sweep_delete(e)


# Event detectors (PLAN A2, server/detectors.py). Each event gives the user one
# popup with Open on that tab; a waiting prompt also gets one line in the
# pane that owns the panel. Replies already reach their pane themselves.
detector_state = detectors.Detectors()
DETECT_S = 1.0


def _panel_label(tab_id: int | None) -> str:
    """The name the user knows a panel by: open-panel --name, registered name,
    then the /brN alias. Must be called without `lock` (short_for_tab)."""
    if tab_id is None:
        return ""
    with lock:
        e = panel_meta_store.get(tab_id)
        if e:
            return e["name"]
        for n, p in panels.items():
            if p.get("tab_id") == tab_id:
                return n
    return short_for_tab(tab_id)


def _owner_pane(tab_id: int) -> str | None:
    with lock:
        e = panel_meta_store.get(tab_id) or {}
        return e.get("owner_pane") or relay_targets.get(tab_id)


# Runs in Claude for Chrome's service worker: the CDP target of the active web
# page in the tab's group (the page an agent is stuck on), as bridge-login does.
ACTIVE_PAGE_JS = """(async () => {
  let t; try { t = await chrome.tabs.get(%d); } catch (_) { return null; }
  let pages = t.groupId >= 0 ? await chrome.tabs.query({groupId: t.groupId}) : [t];
  pages = pages.filter(p => /^https?:/.test(p.url || ''));
  const p = pages.find(p => p.active) || (pages.length === 1 ? pages[0] : null);
  if (!p) return null;
  const m = (await chrome.debugger.getTargets()).find(x => x.tabId === p.id);
  return m ? m.id : null;
})()"""


TAB_TARGET_JS = "chrome.debugger.getTargets().then(ts => (ts.find(x => x.tabId === %d) || {}).id || null)"


def _tab_target(tab: int) -> str | None:
    """The CDP target id of a tab, asked from Claude for Chrome's service
    worker. Used when the extension push has none (not reloaded yet)."""
    targets = _cdp_list()
    for t in targets or []:
        if t.get("type") == "service_worker" and t.get("url", "").startswith(f"chrome-extension://{CFC_EXT_ID}/"):
            try:
                found = _cdp_eval(t, TAB_TARGET_JS % int(tab), _cdp_last.get("host"))
            except Exception:
                continue
            if found:
                return found
    return None


def _active_page_target(tab: int) -> str | None:
    """The user's rule: a sign-in popup opens the page to sign in on, not the
    panel. Read-only CDP; None when Chrome cannot tell."""
    targets = _cdp_list()
    if not targets:
        return None
    host = _cdp_last.get("host")
    for t in targets:
        if t.get("type") == "service_worker" and t.get("url", "").startswith(f"chrome-extension://{CFC_EXT_ID}/"):
            try:
                found = _cdp_eval(t, ACTIVE_PAGE_JS % int(tab), host)
            except Exception:
                continue
            if found:
                return found
    return None


def emit_events(events: list) -> None:
    """Turn detector events into popups (and pane lines). Call without `lock`.
    An event may carry a 4th item, the CDP target of the page it is about."""
    for ev in events:
        kind, tab, detail = ev[:3]
        page = ev[3] if len(ev) > 3 else None
        if kind == "login" and not page and tab is not None:
            page = _active_page_target(tab)
        if not page and tab is not None:
            # The user's rule: an action popup opens the page. The panel's own tab
            # is the page for a prompt; ask Chrome when the push has no id.
            with lock:
                page = _group_target(tab)
            page = page or _tab_target(tab)
        name = _panel_label(tab)
        text, sticky = detectors.message(kind, name, detail)
        print(f"[INFO] event {kind} tab={tab}: {text}", file=sys.stderr)
        notify_user(text, tab_id=page or tab, sticky=sticky, key=f"{kind}-{tab}", kind=EVENT_KINDS[kind])
        pane = _owner_pane(tab) if kind == "prompt" else None
        if pane:
            threading.Thread(target=deliver_reply, args=(pane, text),
                             kwargs={"panel": short_for_tab(tab), "wait_ready": True},
                             daemon=True).start()


# A8: on "I can't sign in to <site>" the bridge signs the agent in with the
# password manager (an optional scripts/bridge-login), then tells the agent to
# go on. Contract: exit 0 signed in, 1 the CLI already showed the user a popup,
# 2 setup/CDP failure (popup shown only if it found the page, i.e. host set).
LOGIN_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge-login"
LOGIN_TIMEOUT_S = 150   # 15 s automatic + up to 120 s for the user's account choice
RESUME_TEXT = "You're signed in now, please continue."
login_running: set = set()   # tabs with a bridge-login in progress
sweep_said: dict = {}        # tab -> last kept-state logged, so each change is logged once


def _resume_panel(tab: int, pane: str | None) -> None:
    """Send the panel the resume line, like browser-bridge message would;
    its reply goes back to the pane that sent the task."""
    with lock:
        to = next((p.get("to") for p in panels.values() if p.get("tab_id") == tab), None) or "browser"
        outbox.append({"id": uuid.uuid4().hex, "from_pane": pane or relay_targets.get(tab) or "%bridge",
                       "text": RESUME_TEXT, "mode": "sidepanel", "to_agent": to, "tab_id": tab,
                       "t_send_queued": time.time()})


def run_login(tab: int, site: str, pane: str | None) -> None:
    """One bridge-login per tab at a time. Logs host and exit code only."""
    with lock:
        if tab in login_running:
            print(f"[INFO] login tab={tab}: already running, skipped", file=sys.stderr)
            return
        login_running.add(tab)
    host, rc = "", None
    try:
        r = subprocess.run([str(LOGIN_SCRIPT), str(tab)], capture_output=True, text=True, timeout=LOGIN_TIMEOUT_S)
        rc = r.returncode
        try:
            host = json.loads(r.stdout.strip().splitlines()[-1]).get("host") or ""
        except (ValueError, IndexError, AttributeError):
            pass
    except subprocess.TimeoutExpired:
        rc = "timeout"
    except Exception as e:
        rc = f"error:{type(e).__name__}"
    finally:
        with lock:
            login_running.discard(tab)
    print(f"[INFO] login tab={tab} host={host or '-'} exit={rc}", file=sys.stderr)
    if rc == 0:
        _resume_panel(tab, pane)
    elif rc != 1 and not host:
        # The CLI did not reach the page, so it showed nothing: the user gets the popup here.
        emit_events([("login", tab, site)])


def signed_out_check_once(now: float) -> None:
    """Reads the target list the close watcher just fetched. A side panel with
    no claude.ai frame for 5 s gets one read of its page text."""
    snap = dict(_cdp_last)
    if snap["targets"] is None or now - snap["ts"] > 3 * DETECT_S:
        return
    with lock:
        todo = detector_state.frames_to_check(snap["targets"], CFC_EXT_ID, now)
    for t in todo:
        tab = int(t["url"].split("tabId=")[1].split("&")[0])
        try:
            text = _cdp_eval(t, detectors.SIGNED_OUT_JS, snap["host"]) or ""
        except Exception as e:
            print(f"[WARN] signed-out check tab {tab}: {e}", file=sys.stderr)
            continue
        with lock:
            events = detector_state.on_panel_text(tab, text)
        emit_events(events)


# Live finding 2026-10-25: in the cic panel, a "Allow Claude to use <site>"
# prompt shows Allow once / Always allow / Deny, but the group title stays
# ✅ (no 🔔). So while a panel is in flight its frame is read for that prompt.
PROMPT_JS = ("[...document.querySelectorAll('button')].some(b => "
             "/^(Allow once|Allow this action)\\b/.test((b.innerText || '').trim()))")
PROMPT_POLL_S = 3.0
prompt_dom: dict = {}   # tab -> True while the frame shows a permission prompt
# Its own debounce state: the extension's push reports the same tab as ✅
# every 3 s, which reset the shared one before the 5 s hold (live 2026-10-25).
dom_prompt_detector = detectors.Detectors()


def prompt_check_once(now: float) -> None:
    snap = dict(_cdp_last)
    if snap["targets"] is None or now - snap["ts"] > 10:
        return
    with lock:
        # In-flight panels, plus named panels: a prompt can outlive a server
        # restart, which empties in_flight.
        busy = {v.get("tab_id") for v in in_flight.values() if isinstance(v, dict) and v.get("tab_id") is not None}
        busy |= {e["tab_id"] for e in panel_meta_store.entries() if not e.get("orphan_since")}
    for gone in [t for t in prompt_dom if t not in busy]:
        prompt_dom.pop(gone, None)
    if not busy:
        return
    panels_by_id = {}
    for t in snap["targets"]:
        m = re.match(rf"chrome-extension://{CFC_EXT_ID}/sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
        if m and int(m.group(1)) in busy:
            panels_by_id[t.get("id")] = int(m.group(1))
    for t in snap["targets"]:
        tab = panels_by_id.get(t.get("parentId"))
        if tab is None or not t.get("url", "").startswith("https://claude.ai/"):
            continue
        try:
            waiting = _cdp_eval(t, PROMPT_JS, snap["host"]) is True
        except Exception:
            continue
        prompt_dom[tab] = waiting
        with lock:
            events = dom_prompt_detector.on_push([{"tab_id": tab, "group_title": ("\U0001f514" if waiting else "") + "x"}], now)
        emit_events(events)


def prompt_watcher() -> None:
    while True:
        time.sleep(PROMPT_POLL_S)
        try:
            prompt_check_once(time.time())
        except Exception as e:
            print(f"[WARN] prompt watcher: {e}", file=sys.stderr)


def signed_out_watcher() -> None:
    while True:
        time.sleep(DETECT_S)
        try:
            signed_out_check_once(time.time())
        except Exception as e:
            print(f"[WARN] signed-out watcher: {e}", file=sys.stderr)


def panel_sweeper() -> None:
    while True:
        time.sleep(SWEEP_S)
        try:
            panel_sweep_once(time.time())
        except Exception as e:
            print(f"[WARN] panel sweep: {e}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeBridge/0.2"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _auth_ok(self) -> bool:
        # /health and /dashboard* are unauthenticated — the dashboard
        # HTML/JS/CSS must load so the operator can paste their token
        # into the page; the page's fetch() calls then carry the bearer.
        if self.path == "/health":
            return True
        if self.path == "/dashboard" or self.path.startswith("/dashboard/"):
            return True
        h = self.headers.get("Authorization", "")
        return h == f"Bearer {TOKEN}"

    def _json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_OPTIONS(self):
        self._empty(204)

    def do_GET(self):
        if not self._auth_ok():
            return self._json(401, {"error": "unauthorized"})

        # Static dashboard files. Served from ~/browser-claude-bridge/ui/dashboard/.
        # GET /dashboard         -> dashboard/index.html
        # GET /dashboard/<file>  -> dashboard/<file> (within the dir, no ..)
        if self.path == "/dashboard" or self.path.startswith("/dashboard/"):
            rel = self.path[len("/dashboard"):].lstrip("/")
            if not rel:
                rel = "index.html"
            # Block any path-traversal attempt.
            if ".." in rel.split("/") or rel.startswith("/"):
                return self._json(403, {"error": "forbidden"})
            base = Path(__file__).resolve().parent.parent / "ui" / "dashboard"
            full = (base / rel).resolve()
            if not str(full).startswith(str(base.resolve())):
                return self._json(403, {"error": "forbidden"})
            if not full.is_file():
                return self._json(404, {"error": f"not found: {rel}"})
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js":   "application/javascript; charset=utf-8",
                ".css":  "text/css; charset=utf-8",
                ".json": "application/json; charset=utf-8",
                ".svg":  "image/svg+xml",
                ".png":  "image/png",
                ".ico":  "image/x-icon",
            }.get(full.suffix, "application/octet-stream")
            data = full.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/health":
            with lock:
                return self._json(200, {
                    "ok": True,
                    "queued": len(outbox),
                    "pending_replies": len(in_flight),
                    "tabs": len(tabs_state["tabs"]),
                    "tabs_updated_age_s": (
                        int(time.time() - tabs_state["updated_at"])
                        if tabs_state["updated_at"] else None
                    ),
                })
        if self.path == "/poll" or self.path.startswith("/poll?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            wanted_mode = (qs.get("mode", ["tab"])[0] or "tab").strip()
            with lock:
                # Pop the first item matching the requested mode.
                for i, item in enumerate(list(outbox)):
                    if item.get("mode", "tab") == wanted_mode:
                        if item.get("tab_id") in cleaning_tabs and wanted_mode != "control":
                            continue  # the sweep is deleting this panel right now
                        del outbox[i]
                        in_flight[item["id"]] = {
                            "from_pane": item["from_pane"],
                            "to_agent": item.get("to_agent"),
                            "tab_id": item.get("tab_id"),
                        }
                        return self._json(200, item)
            return self._empty(204)
        if self.path == "/tabs":
            with lock:
                return self._json(200, dict(tabs_state))
        if self.path == "/pin":
            with lock:
                return self._json(200, dict(pin_state) or {})
        if self.path == "/agents":
            with lock:
                return self._json(200, {"agents": dict(agents)})
        if self.path == "/panels":
            with lock:
                return self._json(200, {"panels": dict(panels)})
        if self.path == "/panels/targets":
            # tab id -> CDP target id of the group's first tab, pushed by the
            # extension. bridge-notify's Open activates the tab with it.
            with lock:
                return self._json(200, {"targets": {
                    str(v["tab_id"]): v["target_id"] for v in discovered_panels.values()
                    if v.get("target_id")}})
        if self.path == "/panels/meta":
            with lock:
                return self._json(200, {"ttl_s": PANEL_TTL_S, "panels": panel_meta_store.entries()})
        if self.path == "/panels/all":
            with lock:
                return self._json(200, {"panels": assign_short_names()})
        if self.path == "/panels/discovered":
            # Read-only view of the discovered cache. The POST handler with
            # the same path is what the extension uses to push panels. Until
            # now a GET returned a generic 404 which was easy to misread as
            # "no panels discovered" rather than "wrong HTTP method".
            with lock:
                return self._json(200, {
                    "discovered": [
                        {"tab_group_title": (d.get("group_title") if isinstance(d, dict) else None) or t,
                         **(d if isinstance(d, dict) else {})}
                        for t, d in discovered_panels.items()
                    ],
                    "count": len(discovered_panels),
                })
        if self.path == "/tmux-panes":
            # Enumerate tmux panes (id + label) via tmux directly. Used by the
            # popup to populate per-row routing dropdowns. Falls back to an
            # empty list if tmux isn't installed or no server is running.
            #
            # Each pane is enriched with:
            #   - connected_browsers: list of browser panels whose effective
            #     relay target equals this pane's @name (so the popup can show
            #     "which browser agents will reply into this pane").
            #   - state: one of "new" | "active" | "finished" | "idle" — derived
            #     from last_prompt_sent_per_pane / last_reply_per_pane.
            try:
                res = subprocess.run(
                    ["tmux", "list-panes", "-a",
                     "-F", "#{pane_id}|#{@name}|#{session_name}:#{window_index}.#{pane_index}|#{pane_current_command}|#{pane_tty}|#{pane_pid}"],
                    capture_output=True, text=True, timeout=3,
                )
                raw_panes = []
                for ln in (res.stdout or "").splitlines():
                    parts = ln.split("|", 5)
                    if len(parts) < 2 or not parts[0].startswith("%"):
                        continue
                    parts = parts + [""] * (6 - len(parts))
                    pane_id = parts[0]
                    raw_panes.append({
                        "pane_id": pane_id,
                        "label": parts[1] or "",
                        "session": parts[2],
                        "cmd": parts[3],
                        "tty": parts[4],
                        "pane_pid": parts[5],
                    })
                # Build a one-shot parent->children map from /proc so we can
                # ask "is any descendant of this pane PID a claude.exe /
                # codex / claude process?" without forking per-pane.
                # Cheap on Linux: one directory listing + comm read per pid.
                proc_children = {}   # ppid -> [pid, ...]
                proc_comm = {}       # pid -> comm string (lowercase)
                try:
                    for entry in os.listdir("/proc"):
                        if not entry.isdigit():
                            continue
                        pid = int(entry)
                        try:
                            with open(f"/proc/{pid}/stat", "rb") as f:
                                stat = f.read().decode("ascii", "replace")
                            # comm is parenthesized; ppid is the field right
                            # after the closing paren. Split on ')' to handle
                            # comms that contain spaces or '('.
                            close = stat.rfind(")")
                            if close < 0:
                                continue
                            comm = stat[stat.find("(") + 1:close]
                            after = stat[close + 2:].split()
                            ppid = int(after[1])
                            proc_comm[pid] = comm.lower()
                            proc_children.setdefault(ppid, []).append(pid)
                        except Exception:
                            continue
                except Exception:
                    pass

                AGENT_COMM_HINTS = ("claude.exe", "claude", "codex", "aider", "gemini")

                # Claude Code writes one ~/.claude/sessions/<windows_pid>.json
                # per running interactive session, containing sessionId, cwd,
                # and startedAt (epoch ms). That's an authoritative mapping
                # from process -> session UUID. We resolve a pane's session
                # by matching its claude.exe Linux process START TIME against
                # the startedAt field across all session JSONs whose cwd
                # matches the pane's agent_cwd. The Linux start time is
                # within a couple seconds of the Windows startedAt (same
                # process, different OS perspective).
                CLAUDE_SESSIONS_DIR = os.path.join(WIN_HOME, ".claude", "sessions")

                def _wsl_path_to_win(wsl_path):
                    m = re.match(r"^/mnt/([a-z])(/.*)?$", wsl_path or "")
                    if not m:
                        return wsl_path or ""
                    return m.group(1).upper() + ":" + (m.group(2) or "").replace("/", "\\")

                def _read_sessions_index():
                    out = []
                    if not os.path.isdir(CLAUDE_SESSIONS_DIR):
                        return out
                    try:
                        for fn in os.listdir(CLAUDE_SESSIONS_DIR):
                            if not fn.endswith(".json"):
                                continue
                            try:
                                with open(os.path.join(CLAUDE_SESSIONS_DIR, fn)) as f:
                                    out.append(json.load(f))
                            except Exception:
                                continue
                    except OSError:
                        pass
                    return out

                # Per-server-process cache of boot time + clock-ticks-per-sec
                # so we can convert /proc/<pid>/stat starttime into epoch.
                try:
                    _CLK_TCK = os.sysconf("SC_CLK_TCK")
                except Exception:
                    _CLK_TCK = 100
                _BTIME = 0
                try:
                    with open("/proc/stat") as _f:
                        for _ln in _f:
                            if _ln.startswith("btime "):
                                _BTIME = int(_ln.split()[1])
                                break
                except Exception:
                    pass

                def _proc_start_epoch(linux_pid):
                    if not linux_pid:
                        return None
                    try:
                        with open(f"/proc/{linux_pid}/stat") as f:
                            stat = f.read()
                        after = stat[stat.rfind(")") + 1:].split()
                        # field 22 in 1-indexed = "starttime" — at index 19
                        # in the post-paren split (fields 3..52).
                        return _BTIME + int(after[19]) / _CLK_TCK
                    except Exception:
                        return None

                def _pane_has_agent_with_cwd(root_pid_str):
                    """(has_agent, cwd, agent_linux_pid) — walks descendants,
                    returns cwd + Linux pid of the first agent-process found.
                    cwd / pid are None if no agent or readlink fails."""
                    try:
                        root = int(root_pid_str)
                    except Exception:
                        return (False, None, None)
                    seen = set()
                    queue = [root]
                    while queue:
                        cur = queue.pop()
                        if cur in seen:
                            continue
                        seen.add(cur)
                        comm = proc_comm.get(cur, "")
                        if any(h in comm for h in AGENT_COMM_HINTS):
                            try:
                                cwd = os.readlink(f"/proc/{cur}/cwd")
                            except Exception:
                                cwd = None
                            return (True, cwd, cur)
                        queue.extend(proc_children.get(cur, []))
                    return (False, None, None)
                now = time.time()
                # Enrichment touches the same shared state /relay-target writes
                # (relay_targets / relay_default_target) plus the new per-pane
                # timestamp maps; hold the global lock to read a consistent snapshot.
                with lock:
                    unified_panels = assign_short_names()
                    targets_snapshot = dict(relay_targets)
                    default_target_snapshot = relay_default_target
                    prompt_ts_snapshot = dict(last_prompt_sent_per_pane)
                    reply_ts_snapshot = dict(last_reply_per_pane)
                    color_snapshot = {
                        tid: entry.get("color")
                        for tid, entry in tab_group_color.items()
                    }
                for p in raw_panes:
                    pane_name = p["label"]
                    pane_id = p["pane_id"]
                    connected: list[dict] = []
                    for pn in unified_panels:
                        tid = pn.get("tab_id")
                        if tid is None:
                            continue
                        eff_target = targets_snapshot.get(tid) or default_target_snapshot
                        if not eff_target:
                            continue
                        # Match: prefer pane_id (always unique). Fall back to
                        # @name for legacy targets stored as just the label.
                        # Multiple panes that share the same @name will ALL
                        # claim the same browser agent under the legacy path —
                        # that's the duplicate-name footgun. Operators should
                        # store targets as pane_id (e.g. "%3") to avoid it.
                        if eff_target.startswith("%"):
                            matched = (eff_target == pane_id)
                        else:
                            matched = bool(pane_name) and (eff_target == pane_name)
                        if matched:
                            connected.append({
                                "short": pn.get("short"),
                                "name": pn.get("name"),
                                "tab_id": tid,
                                "color": color_snapshot.get(tid),
                            })
                    p["connected_browsers"] = connected
                    # State machine for this pane.
                    last_prompt = prompt_ts_snapshot.get(p["pane_id"])
                    last_reply = reply_ts_snapshot.get(p["pane_id"])
                    if last_prompt is None and last_reply is None:
                        state = "new"
                    elif last_prompt is not None and (last_reply is None or last_prompt > last_reply):
                        # Outstanding prompt; "active" only within a 5-minute window.
                        if (now - last_prompt) < 300:
                            state = "active"
                        else:
                            # Prompt is stale and no reply ever came — treat as finished/idle
                            # so the UI stops showing it as "active" forever.
                            state = "finished"
                    else:
                        # last_reply >= last_prompt (or there's a reply but no prompt tracked).
                        state = "finished"
                        if last_reply is not None and (now - last_reply) >= 600:
                            state = "idle"
                    p["state"] = state
                    # Last bridge activity timestamp (max of prompt-out / reply-in).
                    # Kept on the object so the popup can show "Xh ago" on
                    # Inactive rows. Not used to bucket Active anymore — the
                    # popup uses tty_atime_age_s + has_agent_process instead.
                    if last_prompt is None and last_reply is None:
                        p["last_activity_at"] = None
                    elif last_prompt is None:
                        p["last_activity_at"] = last_reply
                    elif last_reply is None:
                        p["last_activity_at"] = last_prompt
                    else:
                        p["last_activity_at"] = max(last_prompt, last_reply)
                    # tty_atime_age_s — seconds since the pane's pty was last
                    # READ by anyone (terminal renders).
                    # tty_mtime_age_s — seconds since the pane's pty was last
                    # WRITTEN to (claude output + operator input arriving).
                    # Popup uses the min of these as the "last activity" time
                    # since either signal beats "I haven't touched this pane".
                    tty = p.get("tty")
                    atime_age = mtime_age = None
                    if tty:
                        try:
                            atime_age = int(now - os.path.getatime(tty))
                            mtime_age = int(now - os.path.getmtime(tty))
                        except Exception:
                            pass
                    p["tty_atime_age_s"] = atime_age
                    p["tty_mtime_age_s"] = mtime_age
                    # has_agent_process — does this pane have a claude.exe /
                    # codex / claude / aider / gemini descendant right now?
                    # When true, also resolve the agent's cwd so the popup
                    # can build a `cd <cwd> && claude --resume ...` command
                    # for the operator to re-open the session in a new pane.
                    has_agent, agent_cwd, agent_pid = _pane_has_agent_with_cwd(p.get("pane_pid"))
                    p["has_agent_process"] = has_agent
                    p["agent_cwd"] = agent_cwd
                    p["_agent_linux_pid"] = agent_pid   # internal — stripped below
                    p["session_id"] = None              # filled in by index pass

                # Session-UUID resolution via ~/.claude/sessions/<wpid>.json.
                # Two-pass:
                #   1. Build candidates by matching pane.agent_cwd (Win form)
                #      against session.cwd, and proc_start_epoch(agent_pid)
                #      against session.startedAt/1000 within a small window.
                #   2. Sort by delta and greedily assign — each session UUID
                #      goes to at most one pane (avoid double-claim when two
                #      panes have nearly-identical start times).
                sessions = _read_sessions_index()
                candidates = []  # (delta_s, pane_idx, session_dict)
                for i, p in enumerate(raw_panes):
                    if not p["has_agent_process"]:
                        continue
                    cwd_wsl = p.get("agent_cwd")
                    agent_pid = p.get("_agent_linux_pid")
                    if not cwd_wsl or not agent_pid:
                        continue
                    cwd_win = _wsl_path_to_win(cwd_wsl).lower()
                    pstart = _proc_start_epoch(agent_pid)
                    if pstart is None:
                        continue
                    for s in sessions:
                        s_cwd = (s.get("cwd") or "").lower()
                        if not s_cwd or s_cwd != cwd_win:
                            continue
                        s_start = (s.get("startedAt") or 0) / 1000.0
                        if s_start <= 0:
                            continue
                        delta = abs(s_start - pstart)
                        if delta <= 10:  # claude.exe usually within ~2s
                            candidates.append((delta, i, s))
                candidates.sort(key=lambda c: c[0])
                used_panes = set()
                # Same session UUID can be assigned to multiple panes —
                # `claude --resume <uuid>` spawns a new Windows pid each
                # time but keeps the original sessionId, so panes that
                # resumed the same chat all map to the same UUID. Only
                # per-pane dedup is needed: each pane takes its closest
                # (proc_start, startedAt) match by delta.
                for delta, i, s in candidates:
                    if i in used_panes:
                        continue
                    uuid = s.get("sessionId")
                    if not uuid:
                        continue
                    raw_panes[i]["session_id"] = uuid
                    # Real-time agent status from the session file:
                    # "busy"  -> claude is processing a turn (active)
                    # "idle"/"ready"/"shell" -> waiting at prompt
                    # The popup colors the row dot from this.
                    raw_panes[i]["agent_status"] = s.get("status")
                    used_panes.add(i)
                # Strip internal helper fields.
                for p in raw_panes:
                    p.pop("_agent_linux_pid", None)

                # Last-session cache. For panes whose claude.exe just
                # exited, the resume command (📋) still needs session_id
                # and cwd — so we keep the most recently resolved values
                # alive in last_session_per_pane and hydrate them onto
                # the pane object when the live agent is gone. The
                # popup uses agent_exited to draw the orange dot and to
                # know the session shouldn't count toward "in use" dedup.
                with lock:
                    for p in raw_panes:
                        sid = p.get("session_id")
                        if sid:
                            last_session_per_pane[p["pane_id"]] = {
                                "session_id": sid,
                                "cwd": p.get("agent_cwd"),
                                "ts": now,
                            }
                    # GC old cache entries
                    for pid in list(last_session_per_pane.keys()):
                        if (now - last_session_per_pane[pid]["ts"]) > LAST_SESSION_TTL_S:
                            del last_session_per_pane[pid]
                    # Hydrate exited panes from cache.
                    for p in raw_panes:
                        if p.get("session_id"):
                            p["agent_exited"] = False
                        else:
                            cached = last_session_per_pane.get(p["pane_id"])
                            if cached:
                                p["session_id"] = cached["session_id"]
                                p["agent_cwd"] = cached["cwd"]
                                p["agent_exited"] = True
                            else:
                                p["agent_exited"] = False

                # Persistent pane names by session UUID. Extension-only:
                #   DRAIN: each /tmux-pane/rename writes
                #     pending_renames_by_pane[pane_id] = (new_name, ts).
                #     Here we look up the pane's session_id and persist.
                #     GC entries older than 60s if their pane vanished
                #     before its session resolved.
                #   RESTORE: if a pane has session_id AND a saved name AND
                #     its current @name doesn't match, re-apply via
                #     `tmux set-option`. This reverts manual tmux renames
                #     and restores names across /exit + --resume cycles.
                #   NO promote-from-@name. The extension is the sole
                #     source of truth for the saved name.
                dirty = False
                pane_by_id = {p["pane_id"]: p for p in raw_panes}
                with lock:
                    # Drain pending renames.
                    expired = []
                    for pid, (new_name, ts) in list(pending_renames_by_pane.items()):
                        p = pane_by_id.get(pid)
                        if p and p.get("session_id"):
                            pane_names_by_session[p["session_id"]] = new_name
                            del pending_renames_by_pane[pid]
                            dirty = True
                        elif (now - ts) > 60:
                            expired.append(pid)
                    for pid in expired:
                        del pending_renames_by_pane[pid]

                    # Restore: extension-saved name wins over current @name.
                    for p in raw_panes:
                        sid = p.get("session_id")
                        if not sid:
                            continue
                        saved = pane_names_by_session.get(sid)
                        if not saved:
                            continue
                        cur = p.get("label") or ""
                        if cur != saved:
                            try:
                                subprocess.run(
                                    ["tmux", "set-option", "-p", "-t", p["pane_id"], "@name", saved],
                                    capture_output=True, timeout=2,
                                )
                                p["label"] = saved
                            except Exception:
                                pass
                    if dirty:
                        save_pane_names()

                return self._json(200, {"panes": raw_panes})
            except FileNotFoundError:
                return self._json(200, {"panes": [], "error": "tmux not installed"})
            except Exception as e:
                import traceback
                sys.stderr.write("[/tmux-panes traceback]\n" + traceback.format_exc())
                sys.stderr.flush()
                return self._json(200, {"panes": [], "error": str(e)})

        if self.path.startswith("/panel/read"):
            # Smux-style atomic READ for a browser panel. Returns the
            # panel's last captured reply text (from last_reply_per_tab)
            # AND sets the read-guard mark for (caller, panel) so a
            # subsequent /panel/type or /panel/keys can succeed.
            #
            #   GET /panel/read?to=/br2&caller=<id>[&lines=N]
            #
            # `to` accepts /brN, registered names, or numeric tab_id.
            # `caller` is opaque (use your pane id, virtual: name, etc).
            # `lines` is accepted for future use but ignored today.
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            to = (qs.get("to", [""])[0] or "").strip()
            caller = (qs.get("caller", [""])[0] or "").strip()
            if not to or not caller:
                return self._json(400, {"error": "to= and caller= required"})
            with lock:
                if to.isdigit():
                    tab_id = int(to)
                else:
                    pinfo = resolve_panel_name(to)
                    if not pinfo or pinfo.get("tab_id") is None:
                        return self._json(404, {"error": f"unknown panel '{to}'"})
                    tab_id = int(pinfo["tab_id"])
                rec = last_reply_per_tab.get(tab_id)
                panel_read_marks[(caller, tab_id)] = time.time()
            return self._json(200, {
                "ok": True,
                "tab_id": tab_id,
                "last_reply": rec,    # may be None if no reply captured yet
                "read_guard_set": True,
                "read_guard_ttl_s": READ_GUARD_TTL_S,
            })

        if self.path == "/skills":
            # Merged list of skills surfaced to the popup:
            #   1. ~/.claude/skills/*/SKILL.md  (Claude Code skill folders)
            #   2. ~/.config/claude-web-bridge/skills/*.md  (bridge-owned flat .md)
            # Each entry includes a display name (frontmatter `name` if present,
            # else filename/folder) and description, plus an absolute path the
            # popup hands back to /skills/dispatch.
            # Single curated root for popup skills:
            # ~/.claude/browser-skills/*.md (Windows side under this setup).
            # Operator drops flat .md files here to make them dispatch-
            # eligible from the extension's SKILLS section. Keeping a
            # dedicated folder prevents Claude Code's general skills
            # (~/.claude/skills/*) from cluttering the popup.
            roots = [
                ("bridge", BROWSER_SKILLS_DIR, True),  # flat .md
            ]
            skills = []
            for source, root, flat in roots:
                try:
                    if not os.path.isdir(root):
                        if source == "bridge":
                            os.makedirs(root, exist_ok=True)
                        continue
                    entries = sorted(os.listdir(root))
                except OSError:
                    continue
                for entry in entries:
                    full = os.path.join(root, entry)
                    if flat:
                        if not entry.endswith(".md") or not os.path.isfile(full):
                            continue
                        fallback_name = entry[:-3]
                        path = full
                    else:
                        skill_md = os.path.join(full, "SKILL.md")
                        if not os.path.isdir(full) or not os.path.isfile(skill_md):
                            continue
                        fallback_name = entry
                        path = skill_md
                    name = fallback_name
                    description = ""
                    try:
                        with open(path, encoding="utf-8") as f:
                            head = f.read(4096)
                        lines = head.split("\n")
                        if lines and lines[0].strip() == "---":
                            for i in range(1, min(len(lines), 30)):
                                if lines[i].strip() == "---":
                                    break
                                if ":" in lines[i]:
                                    k, v = lines[i].split(":", 1)
                                    k = k.strip(); v = v.strip().strip('"').strip("'")
                                    if k == "name" and v:
                                        name = v
                                    elif k == "description" and v:
                                        description = v
                    except Exception:
                        pass
                    skills.append({
                        "name": name,
                        "description": description,
                        "path": path,
                        "source": source,
                    })
            return self._json(200, {"skills": skills})

        if self.path == "/relay-target" or self.path.startswith("/relay-target?"):
            # Per-tab routing target. Mirrors /relay-enabled shape.
            #   /relay-target              -> {default, overrides, ...}
            #   /relay-target?tab_id=N     -> {tab_id, target, source}
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tab_q = qs.get("tab_id", [""])[0]
            with lock:
                if tab_q:
                    try:
                        tid = int(tab_q)
                    except ValueError:
                        return self._json(400, {"error": "tab_id must be an integer"})
                    if tid in relay_targets and relay_targets[tid]:
                        return self._json(200, {
                            "tab_id": tid,
                            "target": relay_targets[tid],
                            "source": "override",
                        })
                    return self._json(200, {
                        "tab_id": tid,
                        "target": relay_default_target,
                        "source": "default",
                    })
                return self._json(200, {
                    "default": relay_default_target,
                    "overrides": {str(k): v for k, v in relay_targets.items()},
                })
        if self.path == "/relay-enabled" or self.path.startswith("/relay-enabled?"):
            # Per-tab relay gate.
            #   /relay-enabled               -> {default, overrides, changed_*}
            #   /relay-enabled?tab_id=N      -> {tab_id, enabled, source}
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tab_q = qs.get("tab_id", [""])[0]
            with lock:
                if tab_q:
                    try:
                        tid = int(tab_q)
                    except ValueError:
                        return self._json(400, {"error": "tab_id must be an integer"})
                    if tid in relay_overrides:
                        return self._json(200, {
                            "tab_id": tid,
                            "enabled": bool(relay_overrides[tid]),
                            "source": "override",
                        })
                    return self._json(200, {
                        "tab_id": tid,
                        "enabled": bool(relay_default_enabled),
                        "source": "default",
                    })
                # No tab_id -> full state, including the legacy `enabled` field
                # so older callers continue to work.
                return self._json(200, {
                    "default": bool(relay_default_enabled),
                    "overrides": {str(k): bool(v) for k, v in relay_overrides.items()},
                    "enabled": bool(relay_default_enabled),    # legacy
                    "changed_at": relay_changed_at,
                    "changed_by": relay_changed_by,
                })
        if self.path == "/panels/ctx":
            with lock:
                return self._json(200, {"now": time.time(), "usage": dict(ctx_usage),
                                        "diag": dict(ctx_diag)})

        if self.path == "/tasks":
            now = time.time()
            with lock:
                for tid in [t for t, r in tasks.items()
                            if r.get("ended_at") and now - r["ended_at"] > TASK_KEEP_S]:
                    tasks.pop(tid, None)
                rows = [{k: v for k, v in r.items() if k != "dispatch_id"} for r in tasks.values()]
            return self._json(200, {"tasks": rows})

        if self.path == "/panels/activity":
            with lock:
                streaming_tabs = sorted({
                    v.get("tab_id") for k, v in in_flight.items()
                    if k in streaming_ids and isinstance(v, dict) and v.get("tab_id") is not None
                })
                working_tabs = sorted({
                    v.get("tab_id") for k, v in in_flight.items()
                    if k not in streaming_ids and isinstance(v, dict) and v.get("tab_id") is not None
                } | {it.get("tab_id") for it in outbox if it.get("tab_id") is not None})
                return self._json(200, {
                    "now": time.time(),
                    "streaming_tab_ids": streaming_tabs,    # pink
                    "working_tab_ids": working_tabs,        # blue (queued or in-flight pre-stream)
                    "last_active_tab": dict(last_active_tab),
                    # Chrome tab-group colour per tab, so surfaces without the
                    # chrome.tabGroups API (the localhost dashboard) can tint
                    # their dots the same way the extension popup does.
                    "group_colors": {
                        str(tid): entry.get("color")
                        for tid, entry in tab_group_color.items()
                        if entry.get("color")
                    },
                    # Tabs whose group is collapsed (hidden in the tab strip).
                    "collapsed_tab_ids": sorted({
                        d["tab_id"] for d in discovered_panels.values()
                        if isinstance(d, dict) and d.get("group_collapsed")
                        and d.get("tab_id") is not None
                    }),
                })
        if self.path.startswith("/panels/") and self.path.endswith("/last-reply"):
            from urllib.parse import unquote
            inner = self.path[len("/panels/"):-len("/last-reply")]
            target = unquote(inner)
            if not target:
                return self._json(400, {"error": "target required"})
            with lock:
                tab_id: int | None = None
                if target.isdigit():
                    tab_id = int(target)
                else:
                    pinfo = resolve_panel_name(target)
                    if pinfo and pinfo.get("tab_id") is not None:
                        tab_id = int(pinfo["tab_id"])
                if tab_id is None:
                    return self._json(404, {"error": f"no captured reply for {target}"})
                rec = last_reply_per_tab.get(tab_id)
                if rec is None:
                    return self._json(404, {"error": f"no captured reply for {target}"})
                return self._json(200, {"tab_id": tab_id, **rec})
        if self.path.startswith("/reply"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            mid = (qs.get("id", [""])[0] or "").strip()
            consume = (qs.get("consume", ["1"])[0] or "1").lower() not in ("0", "false", "no")
            try:
                wait_s = float(qs.get("wait", ["0"])[0] or "0")
            except ValueError:
                wait_s = 0.0
            wait_s = max(0.0, min(wait_s, 300.0))  # clamp to 5 minutes
            if not mid:
                return self._json(400, {"error": "id required"})
            # Long-poll: block on Condition var until the reply lands or wait_s expires.
            deadline = time.time() + wait_s
            with reply_cv:
                rec = replies_buffer.get(mid)
                while rec is None and time.time() < deadline:
                    remaining = deadline - time.time()
                    reply_cv.wait(timeout=min(remaining, 30.0))
                    rec = replies_buffer.get(mid)
                if rec is not None:
                    if consume:
                        replies_buffer.pop(mid, None)
                    return self._json(200, {"id": mid, **rec})
                pending = mid in in_flight
            return self._json(404, {"error": "not found", "pending": pending})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        # ACL read in /send and assignment in /relay-target both touch
        # these module-level dicts; declaring global up-front avoids a
        # parser-level "used prior to global declaration" error.
        global relay_default_target, relay_targets
        if not self._auth_ok():
            return self._json(401, {"error": "unauthorized"})
        try:
            data = self._read_json()
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})

        if self.path == "/send":
            from_pane = (data.get("from_pane") or "").strip()
            text = data.get("text") or ""
            auto_submit = data.get("auto_submit", True) if data.get("auto_submit") is not None else True
            keys_only = bool(data.get("keys_only", False))
            keys = data.get("keys") or []
            if not from_pane:
                return self._json(400, {"error": "from_pane required"})
            if not text and not keys_only:
                return self._json(400, {"error": "text required (or keys_only=true with keys)"})
            # Friendly-name resolution: to_panel="browser-research" or "%2" →
            # fills in mode=sidepanel, to=<agent>, tab_id=<panel>. Accepts both
            # registered names (panels.json) and short %N aliases assigned in
            # sorted order across registered + discovered panels.
            to_panel = (data.get("to_panel") or "").strip()
            if to_panel:
                with lock:
                    pinfo = resolve_panel_name(to_panel)
                if not pinfo:
                    with lock:
                        rows = assign_short_names()
                        known = [
                            f"{e['short']} {e['name']} (kind={e.get('kind','?')}, tab_id={e.get('tab_id','?')})"
                            for e in rows
                        ]
                    # Build a self-serve diagnostic: tell the caller exactly
                    # what we resolved against, why the lookup failed, and
                    # the next step (open a Claude for Chrome side panel in
                    # the daemon's Chrome, then retry).
                    hint = (
                        "open Claude for Chrome side panel in the bridge Chrome window, "
                        "then re-run. If panels exist but the name is wrong, pick one "
                        "from the list above or use the %N alias."
                    ) if rows else (
                        "no panels are known. Open Claude for Chrome in the bridge Chrome "
                        "window so the extension can push panel info to the bridge."
                    )
                    return self._json(400, {
                        "error": f"unknown panel '{to_panel}'",
                        "known_count": len(rows),
                        "known_panels": known,
                        "hint": hint,
                    })
                data.setdefault("mode", "sidepanel")
                data.setdefault("to", pinfo.get("to") or "browser")
                data.setdefault("tab_id", pinfo["tab_id"])

                # Dispatch ACL — the popup's per-panel relay-target dropdown
                # also acts as the OWNER. Only that tmux pane (and a few
                # privileged virtual: callers) may POST /send to this panel.
                # Defends against accidental cross-routing like the
                # 2026-05-11 incident where orchestrator dispatched to a
                # panel the operator had assigned to error-log.
                #
                # Override:
                #   - force=true in the request body (CLI --force) skips
                #     the check but logs the breach with the offender's
                #     identity for audit.
                #   - virtual:* callers (cli wrappers, scheduled jobs)
                #     bypass the check by design — they're tools the
                #     operator runs explicitly, not autonomous agents.
                force = bool(data.get("force", False))
                if not force and not from_pane.startswith("virtual:"):
                    tab_id_for_acl = pinfo.get("tab_id")
                    with lock:
                        owner = relay_targets.get(tab_id_for_acl) or relay_default_target
                    if owner and owner != from_pane:
                        return self._json(403, {
                            "error": (
                                f"forbidden: panel '{pinfo.get('name', to_panel)}' is assigned "
                                f"to '{owner}' in the popup. Caller '{from_pane}' cannot dispatch to it."
                            ),
                            "panel": pinfo.get("name", to_panel),
                            "owner": owner,
                            "caller": from_pane,
                            "hint": (
                                "Either dispatch through the owning tmux pane, or change the "
                                "Default/Per-row relay target in the popup, or re-send with "
                                "force=true (will be logged)."
                            ),
                        })
                elif force and not from_pane.startswith("virtual:"):
                    tab_id_for_acl = pinfo.get("tab_id")
                    with lock:
                        owner = relay_targets.get(tab_id_for_acl) or relay_default_target
                    print(
                        f"[acl-override] {from_pane} dispatched to "
                        f"panel '{pinfo.get('name', to_panel)}' "
                        f"(owner={owner}) with force=true",
                        file=sys.stderr,
                    )
            mode = (data.get("mode") or "tab").strip()
            if mode not in ("tab", "sidepanel"):
                return self._json(400, {"error": "mode must be 'tab' or 'sidepanel'"})
            # For sidepanel mode, require an agent name (from `to` field).
            to_agent = (data.get("to") or "").strip() or None
            if mode == "sidepanel":
                if not to_agent:
                    return self._json(400, {"error": "sidepanel mode requires 'to' field naming a registered agent"})
                with lock:
                    if to_agent not in agents:
                        return self._json(400, {
                            "error": f"unknown agent '{to_agent}'. Registered: {sorted(agents.keys())}",
                        })
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "text": text,
                "mode": mode,
                "t_send_queued": time.time(),
            }
            if to_agent:
                item["to_agent"] = to_agent
            if not auto_submit:
                item["auto_submit"] = False
            if keys_only:
                item["keys_only"] = True
            if keys:
                item["keys"] = keys
            task_id = str(data.get("task_id") or "").strip()
            if task_id:
                item["task_id"] = task_id  # the daemon reads live progress only for task messages
            if "tab_id" in data and data["tab_id"] is not None:
                try:
                    item["tab_id"] = int(data["tab_id"])
                except (TypeError, ValueError):
                    return self._json(400, {"error": "tab_id must be an integer"})
            if data.get("tab_match"):
                item["tab_match"] = str(data["tab_match"])
            # Apply pin if no explicit target was given.
            with lock:
                if "tab_id" not in item and "tab_match" not in item:
                    if pin_state.get("url_match"):
                        item["tab_match"] = pin_state["url_match"]
                outbox.append(item)
                if task_id:
                    note_task_send(task_id, item["id"], from_pane,
                                   short_for_tab(item.get("tab_id")) or to_panel, data)
                # Track the most recent outbound prompt timestamp per source
                # pane so GET /tmux-panes can compute the per-pane state
                # ("new" -> "active" -> "finished" -> "idle"). Updated for
                # every /send that carries a from_pane (validated above).
                last_prompt_sent_per_pane[from_pane] = time.time()
            return self._json(200, {"id": item["id"], "queued": True, "pin_applied": bool(item.get("tab_match") and not data.get("tab_match"))})

        if self.path == "/panel/send-now":
            # Fast-path: bundle read + type + read + keys-Enter into ONE
            # HTTP round-trip. Skips the read-guard (the atomic call has
            # no window for state drift between steps). Used by skills /
            # dashboards that don't need the per-step verify discipline.
            # Same delivery semantics as /panel/type + /panel/keys Enter:
            # daemon receives a combined insert+click item and (if
            # from_pane is a real %N pane) replies flow back via
            # tmux-bridge into that pane automatically.
            #
            # Body: {from_pane, to_panel, text}
            from_pane = (data.get("from_pane") or "").strip()
            to_panel = (data.get("to_panel") or "").strip()
            text = data.get("text") or ""
            if not from_pane:
                return self._json(400, {"error": "from_pane required"})
            if not to_panel:
                return self._json(400, {"error": "to_panel required"})
            if not text:
                return self._json(400, {"error": "text required"})
            with lock:
                pinfo = resolve_panel_name(to_panel)
                if not pinfo or pinfo.get("tab_id") is None:
                    return self._json(404, {"error": f"unknown panel '{to_panel}'"})
                tab_id = int(pinfo["tab_id"])
                item = {
                    "id": secrets.token_hex(8),
                    "from_pane": from_pane,
                    "to_panel": to_panel,
                    "to_agent": pinfo.get("to") or "browser",
                    "tab_id": tab_id,
                    "mode": "sidepanel",
                    "text": text,
                    "auto_submit": True,
                    "keys_only": False,
                    "enqueued_at": time.time(),
                }
                outbox.append(item)
                in_flight[item["id"]] = {
                    "from_pane": from_pane,
                    "to_agent": item["to_agent"],
                    "tab_id": tab_id,
                }
            return self._json(200, {
                "ok": True, "id": item["id"], "queued": True,
                "tab_id": tab_id,
                "fast_path": True,
            })

        if self.path == "/panel/type" or self.path == "/panel/keys":
            # Smux-style atomic /panel/type and /panel/keys.
            #   POST /panel/type body: {to_panel, from_pane, text}
            #     -> types text into the panel's Claude side panel input,
            #        does NOT press Enter (auto_submit=false). Caller must
            #        follow up with /panel/keys ... key=Enter to submit.
            #   POST /panel/keys body: {to_panel, from_pane, key}
            #     -> sends a single special key (Enter, Escape, etc.) or
            #        a list of keys.
            #
            # Both endpoints enforce the read-guard: a /panel/read for
            # the same (caller=from_pane, panel=tab_id) within the last
            # READ_GUARD_TTL_S seconds is required. On success the read
            # mark is CLEARED — caller must read again before next op.
            #
            # Reply routing is unchanged: when the extension POSTs /reply
            # back, the existing deliver_reply path lands it INTO the
            # caller's tmux pane via `tmux send-keys` (smux-style).
            # Virtual: callers still hit the id-matched mailbox.
            is_type = self.path == "/panel/type"
            from_pane = (data.get("from_pane") or "").strip()
            to_panel = (data.get("to_panel") or "").strip()
            if not from_pane:
                return self._json(400, {"error": "from_pane required"})
            if not to_panel:
                return self._json(400, {"error": "to_panel required"})
            text = data.get("text") or ""
            keys = data.get("keys") or ([data.get("key")] if data.get("key") else [])
            if is_type and not text:
                return self._json(400, {"error": "text required"})
            if not is_type and not keys:
                return self._json(400, {"error": "key or keys required"})

            with lock:
                pinfo = resolve_panel_name(to_panel)
                if not pinfo or pinfo.get("tab_id") is None:
                    return self._json(404, {"error": f"unknown panel '{to_panel}'"})
                tab_id = int(pinfo["tab_id"])
                # Read-guard check
                mark_ts = panel_read_marks.get((from_pane, tab_id))
                if not mark_ts or (time.time() - mark_ts) > READ_GUARD_TTL_S:
                    return self._json(409, {
                        "error": "read-guard not satisfied",
                        "hint": f"GET /panel/read?to={to_panel}&caller={from_pane} first",
                        "ttl_s": READ_GUARD_TTL_S,
                    })

            # Build the dispatch item. Two paths:
            #
            #   /panel/type
            #     Stash text in pending_panel_text[tab_id] for a later
            #     /panel/keys Enter to consume. DO NOT enqueue an outbox
            #     item — the daemon's Input.insertText alone gets reverted
            #     by Claude's React controlled-component on next WS close,
            #     so a "type-only" dispatch never lands persistently. The
            #     combined insert+click only works in one round-trip.
            #
            #   /panel/keys with key=Enter
            #     Pop pending_panel_text[tab_id] and enqueue a combined
            #     text+auto_submit=true item (daemon path: insert text →
            #     click Send). Survives React because both happen in one
            #     CDP session.
            #
            #   /panel/keys with other keys (Escape, Tab, …)
            #     Raw key dispatch — daemon uses Input.dispatchKeyEvent.
            #     No stashed-text consumption.
            with lock:
                if is_type:
                    pending_panel_text[tab_id] = text
                    panel_read_marks.pop((from_pane, tab_id), None)
                    return self._json(200, {
                        "ok": True, "queued": False, "staged": True,
                        "op": "type", "tab_id": tab_id,
                        "read_guard_cleared": True,
                        "note": "text staged; /panel/keys Enter will submit",
                    })

                # /panel/keys path
                is_enter = (keys == ["Enter"] or keys == ["enter"])
                staged = pending_panel_text.pop(tab_id, None) if is_enter else None

                item = {
                    "id": secrets.token_hex(8),
                    "from_pane": from_pane,
                    "to_panel": to_panel,
                    "to_agent": pinfo.get("to") or "browser",
                    "tab_id": tab_id,
                    "mode": "sidepanel",
                    "enqueued_at": time.time(),
                }
                if is_enter and staged:
                    # Combined insert+click — single CDP round-trip
                    item["text"] = staged
                    item["auto_submit"] = True
                    item["keys_only"] = False
                elif is_enter:
                    # Enter with no staged text — best-effort raw Enter
                    item["text"] = ""
                    item["auto_submit"] = False
                    item["keys_only"] = True
                    item["keys"] = keys
                else:
                    # Non-Enter keys (Escape, Tab, …)
                    item["text"] = ""
                    item["auto_submit"] = False
                    item["keys_only"] = True
                    item["keys"] = keys

                outbox.append(item)
                panel_read_marks.pop((from_pane, tab_id), None)
                in_flight[item["id"]] = {
                    "from_pane": from_pane,
                    "to_agent": item["to_agent"],
                    "tab_id": tab_id,
                }
            return self._json(200, {
                "ok": True, "id": item["id"], "queued": True,
                "op": "keys",
                "tab_id": tab_id,
                "read_guard_cleared": True,
                "used_staged_text": bool(is_enter and staged),
            })

        if self.path == "/reply":
            mid = data.get("id") or ""
            text = data.get("text") or ""
            from_pane = data.get("from_pane") or ""
            from_agent = (data.get("from_agent") or "").strip() or None
            replied_tab_id: int | None = None
            reply_events: list = []
            with lock:
                if mid in in_flight:
                    rec = in_flight.pop(mid)
                    # rec may be a string (legacy) or a dict (new shape)
                    if isinstance(rec, dict):
                        from_pane = rec.get("from_pane") or from_pane
                        from_agent = rec.get("to_agent") or from_agent
                        replied_tab_id = rec.get("tab_id")
                    else:
                        from_pane = rec or from_pane
                if replied_tab_id is not None:
                    last_active_tab[int(replied_tab_id)] = time.time()
                    # 2026-05-10: do NOT overwrite last_reply_per_tab with a
                    # bridge-error payload. The agent's actual reply may settle
                    # later (e.g. a slow navigation finishing after the wedge
                    # timeout fired). Keep the previous real reply visible to
                    # /panels/<tab>/last-reply consumers; route the error only
                    # to the dispatch caller via the long-poll path below.
                    is_error = text.startswith("[bridge error]")
                    # Extension control results ("[control:ok] tabGroup.rename
                    # ...") and type/keys acks are not the panel's answer;
                    # storing them made `read` return a rename confirmation
                    # (2026-10-04). Their callers read them by dispatch id.
                    is_ack = text.startswith(("[control:", "[keys-only dispatch ok", "[type-only dispatch ok"))
                    if not is_error and not is_ack:
                        last_reply_per_tab[int(replied_tab_id)] = {
                            "text": text,
                            "ts": time.time(),
                            "len": len(text),
                        }
                        reply_events = detector_state.on_reply(int(replied_tab_id), text, time.time())
                    # Auto-promote: discovered chat → registered panel on its
                    # first SUCCESSFUL reply. Uses the tab group title as the
                    # registered name, persists to panels.json so it survives
                    # server restarts and shows green permanently.
                    if not is_error:
                        existing_tab_ids = {p.get("tab_id") for p in panels.values()}
                        if int(replied_tab_id) not in existing_tab_ids:
                            for gt, info in discovered_panels.items():
                                gt = info.get("group_title") or gt
                                if info.get("tab_id") == int(replied_tab_id) and gt and gt not in panels:
                                    panels[gt] = {
                                        "to": (from_agent or rec.get("to_agent") if isinstance(rec, dict) else "browser") or "browser",
                                        "tab_id": int(replied_tab_id),
                                        "auto_registered_at": time.time(),
                                    }
                                    save_panels(panels)
                                    break
                streaming_ids.discard(mid)
                note_task_reply(mid, text.startswith("[bridge error]"))
            if reply_events:
                others = [e for e in reply_events if e[0] != "login"]
                login = next((e for e in reply_events if e[0] == "login"), None)
                if login:
                    with lock:  # also holds off password-field popups for this panel
                        popup = detector_state.login_popup(login[1], login[2], time.time())
                    if LOGIN_SCRIPT.exists():
                        threading.Thread(target=run_login, args=(login[1], login[2], from_pane),
                                         daemon=True).start()
                    else:
                        others += popup
                if others:
                    threading.Thread(target=emit_events, args=(others,), daemon=True).start()
            if not from_pane:
                return self._json(400, {"error": "unknown id and no from_pane"})
            # Virtual panes: buffer in-memory for GET /reply?id=... instead of
            # pushing into a tmux pane. Used by lead agents that aren't running
            # inside tmux (e.g. claude-code in plain PowerShell).
            if from_pane.startswith("virtual:"):
                daemon_timing = data.get("daemon_timing") or {}
                with reply_cv:
                    replies_buffer[mid] = {
                        "text": text,
                        "from_agent": from_agent,
                        "from_pane": from_pane,
                        "ts": time.time(),
                        "t_buffered": time.time(),
                        "daemon_timing": daemon_timing,
                    }
                    reply_cv.notify_all()
                return self._json(200, {"buffered": True, "id": mid, "to": from_pane})
            # Browser replies get the single-header format naming the panel
            # that spoke. "[bridge error] ..." becomes "error: ..." so the
            # header is not immediately followed by a second bracketed tag.
            body = text
            if body.startswith("[bridge error]"):
                body = "error:" + body[len("[bridge error]"):]
            # Type/keys acks and extension control results are for the CLI,
            # which reads them by id; pasted into the pane they were noise
            # (and one landed in the operator's bash prompt, 2026-10-04).
            if body.startswith(("[control:", "[keys-only dispatch ok", "[type-only dispatch ok")):
                return self._json(200, {"delivered": False, "reason": "ack, not pasted"})

            # 2026-10-04: a reply used to be pasted at once, on top of whatever
            # the operator was typing (and into bash mode, where it ran as a
            # command). It now waits for the prompt to clear, so delivery runs
            # in its own thread: blocking this handler that long timed out the
            # caller (the 2026-09 BrokenPipeError).
            # agent-panel (2026-11-06): its agents are not Chrome panels in this
            # registry, so the hub names the speaker itself ("/ap1").
            panel_label = data.get("panel") if re.fullmatch(r"/ap\d+", str(data.get("panel") or "")) else None
            def _deliver(pane=from_pane, body=body, tab=replied_tab_id):
                ok, detail = deliver_reply(pane, body, panel=panel_label or short_for_tab(tab),
                                           wait_ready=True, max_wait=REPLY_WAIT_S)
                print(f"[INFO] reply into {pane}: {detail}", file=sys.stderr)
                if ok:
                    # Lets GET /tmux-panes flip the pane from "active" to "finished".
                    with lock:
                        last_reply_per_pane[pane] = time.time()
            threading.Thread(target=_deliver, daemon=True).start()
            return self._json(200, {"delivered": "queued", "to": from_pane, "agent": from_agent})

        if self.path == "/agents/create":
            name = (data.get("name") or "").strip()
            if not name:
                return self._json(400, {"error": "name required"})
            if not name.replace("-", "").replace("_", "").isalnum():
                return self._json(400, {"error": "name must be alnum/dash/underscore"})
            with lock:
                if name in agents:
                    return self._json(409, {"error": f"agent '{name}' already exists"})
                # Port: caller-provided or auto-assigned from range.
                req_port = data.get("port")
                if req_port is not None:
                    try: port = int(req_port)
                    except (TypeError, ValueError):
                        return self._json(400, {"error": "port must be int"})
                    if any(info.get("port") == port for info in agents.values()):
                        return self._json(409, {"error": f"port {port} already in use"})
                else:
                    try: port = next_free_port()
                    except RuntimeError as e:
                        return self._json(409, {"error": str(e)})
                # Profile dir: caller-provided or default scheme.
                profile_dir = (data.get("profile_dir") or
                               f"%LOCALAPPDATA%\\chrome-bridge\\{name}")
                entry = {
                    "profile_dir": profile_dir,
                    "port": port,
                    "registered_at": time.time(),
                    "note": (data.get("note") or "")[:200],
                }
                agents[name] = entry
                save_agents(agents)
            return self._json(200, {"created": name, **entry})

        if self.path == "/agents/rename":
            old = (data.get("old") or "").strip()
            new = (data.get("new") or "").strip()
            if not old or not new:
                return self._json(400, {"error": "old and new required"})
            with lock:
                if old not in agents:
                    return self._json(404, {"error": f"unknown agent: {old}"})
                if new in agents and new != old:
                    return self._json(409, {"error": f"name '{new}' already in use"})
                agents[new] = agents.pop(old)
                save_agents(agents)
            return self._json(200, {"renamed": True, "old": old, "new": new})

        if self.path == "/agents/unregister":
            name = (data.get("name") or "").strip()
            with lock:
                gone = agents.pop(name, None)
                save_agents(agents)
            return self._json(200, {"unregistered": name, "existed": gone is not None})

        if self.path == "/relay-enabled":
            # Body shapes:
            #   {tab_id: N, enabled: true/false, by?: str}  -> set per-tab override
            #   {tab_id: N, enabled: null,       by?: str}  -> clear override (revert to default)
            #   {enabled: true/false,            by?: str}  -> flip the default (legacy global)
            if "enabled" not in data and "tab_id" not in data:
                return self._json(400, {"error": "body must include 'enabled' (bool) or 'tab_id' + 'enabled'"})
            by = str(data.get("by") or "popup")
            tab_raw = data.get("tab_id")
            global relay_default_enabled, relay_overrides, relay_changed_at, relay_changed_by
            with lock:
                relay_changed_at = time.time()
                relay_changed_by = by
                if tab_raw is None:
                    # No tab specified — flip the default
                    relay_default_enabled = bool(data.get("enabled"))
                    save_relay_state()
                    return self._json(200, {
                        "default": relay_default_enabled,
                        "overrides": {str(k): bool(v) for k, v in relay_overrides.items()},
                        "scope": "default",
                        "changed_at": relay_changed_at,
                        "changed_by": relay_changed_by,
                    })
                try:
                    tid = int(tab_raw)
                except (TypeError, ValueError):
                    return self._json(400, {"error": "tab_id must be an integer"})
                enabled_val = data.get("enabled")
                if enabled_val is None:
                    relay_overrides.pop(tid, None)
                    save_relay_state()
                    return self._json(200, {
                        "tab_id": tid,
                        "cleared": True,
                        "effective_enabled": bool(relay_default_enabled),
                        "source": "default",
                        "scope": "override",
                        "changed_at": relay_changed_at,
                        "changed_by": relay_changed_by,
                    })
                relay_overrides[tid] = bool(enabled_val)
                save_relay_state()
                return self._json(200, {
                    "tab_id": tid,
                    "enabled": relay_overrides[tid],
                    "source": "override",
                    "scope": "override",
                    "changed_at": relay_changed_at,
                    "changed_by": relay_changed_by,
                })

        if self.path == "/relay-target":
            # Body shapes:
            #   {target: "label"}                       -> set the default target
            #   {tab_id: N, target: "label"}            -> set a per-tab override
            #   {tab_id: N, target: null | ""}          -> clear the override
            # NB: relay_changed_at/relay_changed_by were already declared global
            # earlier in this function (by the /relay-enabled handler), so this
            # branch only needs to declare the additional names.
            by = str(data.get("by") or "popup")
            tab_raw = data.get("tab_id")
            tgt_raw = data.get("target")
            # relay_default_target / relay_targets are declared global at
            # the top of do_POST so both /send (read) and this branch (write)
            # can touch them without a parser-level conflict.
            with lock:
                relay_changed_at = time.time()
                relay_changed_by = by
                if tab_raw is None:
                    # set the default
                    if tgt_raw is None or str(tgt_raw).strip() == "":
                        return self._json(400, {"error": "target required (or pass tab_id+target=null to clear an override)"})
                    relay_default_target = str(tgt_raw).strip()
                    save_relay_state()
                    return self._json(200, {
                        "default": relay_default_target,
                        "overrides": {str(k): v for k, v in relay_targets.items()},
                        "scope": "default",
                    })
                try:
                    tid = int(tab_raw)
                except (TypeError, ValueError):
                    return self._json(400, {"error": "tab_id must be an integer"})
                if tgt_raw is None or str(tgt_raw).strip() == "":
                    relay_targets.pop(tid, None)
                    save_relay_state()
                    return self._json(200, {
                        "tab_id": tid,
                        "cleared": True,
                        "effective_target": relay_default_target,
                        "source": "default",
                        "scope": "override",
                    })
                relay_targets[tid] = str(tgt_raw).strip()
                save_relay_state()
                return self._json(200, {
                    "tab_id": tid,
                    "target": relay_targets[tid],
                    "source": "override",
                    "scope": "override",
                })

        if self.path == "/tmux-pane/rename":
            # Set the tmux user-option @name on a specific pane.
            # Body: {"pane_id": "%N", "new_name": "..."}
            # The popup reads @name (not pane_title) when labelling panes, so
            # this is how renames from the popup take effect. Names are kept
            # tight (alnum/dash/underscore, <=32 chars) because they appear in
            # every popup row and per-pane routing dropdown.
            pane_id = (data.get("pane_id") or "").strip()
            new_name = (data.get("new_name") or "").strip()
            if not re.match(r"^%\d+$", pane_id):
                return self._json(400, {"error": "pane_id must match ^%\\d+$"})
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", new_name):
                return self._json(400, {"error": "new_name must match ^[A-Za-z0-9_-]{1,32}$"})
            try:
                res = subprocess.run(
                    ["tmux", "set-option", "-p", "-t", pane_id, "@name", new_name],
                    capture_output=True, text=True, timeout=3,
                )
            except FileNotFoundError:
                return self._json(500, {"error": "tmux binary not found"})
            except subprocess.TimeoutExpired:
                return self._json(500, {"error": "tmux set-option timed out"})
            if res.returncode != 0:
                err = (res.stderr or "").strip() or "tmux set-option failed"
                print(f"[ERROR] /tmux-pane/rename pane={pane_id} new_name={new_name}: {err}", flush=True)
                return self._json(500, {"error": err})
            # Queue the rename for the next /tmux-panes poll to persist
            # against the pane's session_id (resolved during that poll).
            # Acts as the SOLE entry point for changing the saved name —
            # manual `tmux set-option @name` outside the extension gets
            # reverted by the restore loop in /tmux-panes.
            with lock:
                pending_renames_by_pane[pane_id] = (new_name, time.time())
            print(f"[INFO] /tmux-pane/rename pane={pane_id} new_name={new_name}", flush=True)
            return self._json(200, {"ok": True, "pane_id": pane_id, "new_name": new_name})

        if self.path == "/tmux-pane/delete":
            # Kill a tmux pane by id. Body: {"pane_id": "%N"}.
            # The pane_id regex is tight so we never hand arbitrary text to
            # `tmux kill-pane -t` (defense against injection / typos).
            # Caller-side (popup) is expected to confirm with the operator
            # AND extra-warn when the pane has a live agent process — the
            # server does not duplicate that check (kill is the operator's
            # explicit decision).
            pane_id = (data.get("pane_id") or "").strip()
            if not re.match(r"^%\d+$", pane_id):
                return self._json(400, {"error": "pane_id must match ^%\\d+$"})
            try:
                res = subprocess.run(
                    ["tmux", "kill-pane", "-t", pane_id],
                    capture_output=True, text=True, timeout=3,
                )
            except FileNotFoundError:
                return self._json(500, {"error": "tmux binary not found"})
            except subprocess.TimeoutExpired:
                return self._json(500, {"error": "tmux kill-pane timed out"})
            if res.returncode != 0:
                err = (res.stderr or "").strip() or "tmux kill-pane failed"
                print(f"[ERROR] /tmux-pane/delete pane={pane_id}: {err}", flush=True)
                return self._json(500, {"error": err})
            print(f"[INFO] /tmux-pane/delete pane={pane_id}", flush=True)
            return self._json(200, {"ok": True, "pane_id": pane_id})

        if self.path == "/skills/dispatch":
            # Send a skill prompt to a tmux pane via send-keys.
            # Body: {"pane_id": "%N", "skill_path": "<abs-path>"}.
            # We send a single-line prompt that points claude at the file
            # (rather than typing the body inline) so multi-line skills
            # don't trigger premature Enter or take seconds to type.
            # skill_path is validated to live under one of the allowed
            # skill roots to keep this from becoming a generic file
            # disclosure path.
            pane_id = (data.get("pane_id") or "").strip()
            skill_path = (data.get("skill_path") or "").strip()
            if not re.match(r"^%\d+$", pane_id):
                return self._json(400, {"error": "pane_id must match ^%\\d+$"})
            allowed_roots = [
                BROWSER_SKILLS_DIR,
            ]
            try:
                resolved = os.path.realpath(skill_path)
            except Exception:
                return self._json(400, {"error": "invalid skill_path"})
            if not any(resolved.startswith(r + os.sep) for r in allowed_roots):
                return self._json(400, {"error": "skill_path outside allowed roots"})
            if not os.path.isfile(resolved):
                return self._json(404, {"error": "skill file not found"})
            prompt = f"Please follow the skill defined in {skill_path}"
            try:
                # Two send-keys calls: the prompt text, then Enter.
                # Splitting avoids tmux parsing the literal text for key
                # names (e.g. a stray "C-c" in a skill path would otherwise
                # be interpreted as Ctrl-C).
                r1 = subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "-l", prompt],
                    capture_output=True, text=True, timeout=3,
                )
                if r1.returncode != 0:
                    err = (r1.stderr or "").strip() or "tmux send-keys (text) failed"
                    print(f"[ERROR] /skills/dispatch pane={pane_id}: {err}", flush=True)
                    return self._json(500, {"error": err})
                r2 = subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "Enter"],
                    capture_output=True, text=True, timeout=3,
                )
                if r2.returncode != 0:
                    err = (r2.stderr or "").strip() or "tmux send-keys (Enter) failed"
                    print(f"[ERROR] /skills/dispatch pane={pane_id}: {err}", flush=True)
                    return self._json(500, {"error": err})
            except FileNotFoundError:
                return self._json(500, {"error": "tmux binary not found"})
            except subprocess.TimeoutExpired:
                return self._json(500, {"error": "tmux send-keys timed out"})
            print(f"[INFO] /skills/dispatch pane={pane_id} skill={resolved}", flush=True)
            return self._json(200, {"ok": True, "pane_id": pane_id, "skill_path": resolved})

        if self.path == "/tmux-pane/announce-target":
            pane = (data.get("pane") or "").strip() or None
            global announce_pin, announce_pin_at
            with lock:
                announce_pin = pane
                announce_pin_at = time.time()
            save_announce_pin(pane)
            print(f"[INFO] announce target pinned to {pane or '(cleared)'}",
                  file=sys.stderr)
            return self._json(200, {"ok": True, "pane": pane})

        if self.path == "/tmux-pane/heartbeat":
            # Mark a tmux pane as actively used (e.g. the agent's
            # UserPromptSubmit hook fires this on every user turn). The state
            # machine reads `last_prompt_sent_per_pane` and within 5 min flips
            # the pane to state="active" — which the popup renders as the
            # "actively chatting" dot. Idempotent; only writes a timestamp.
            pane_id = (data.get("pane_id") or "").strip()
            if not re.match(r"^%\d+$", pane_id):
                return self._json(400, {"error": "pane_id must match ^%\\d+$"})
            with lock:
                last_prompt_sent_per_pane[pane_id] = time.time()
                snapshot = dict(last_prompt_sent_per_pane)
            save_heartbeats(snapshot)
            return self._json(200, {"ok": True, "pane_id": pane_id})

        if self.path == "/pin":
            if data.get("clear"):
                with lock:
                    pin_state.clear()
                    save_pin({})
                return self._json(200, {"cleared": True})
            url_match = (data.get("url_match") or "").strip()
            if not url_match:
                return self._json(400, {"error": "url_match required (or send {clear:true})"})
            new_pin = {
                "url_match": url_match,
                "title_hint": data.get("title_hint", ""),
                "set_at": time.time(),
            }
            with lock:
                pin_state.clear()
                pin_state.update(new_pin)
                save_pin(new_pin)
            return self._json(200, {"pinned": new_pin})

        if self.path == "/tabgroups/rename":
            from_title = (data.get("from") or "").strip()
            to_title = (data.get("to") or "").strip()
            from_pane = (data.get("from_pane") or "%bridge").strip()
            tab_id_in = data.get("tab_id")
            if not to_title or (not from_title and tab_id_in is None):
                return self._json(400, {"error": "to required, plus either from or tab_id"})
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "tabGroup.rename",
                "from_title": from_title,
                "to_title": to_title,
            }
            if tab_id_in is not None:
                try:
                    item["tab_id"] = int(tab_id_in)
                except (TypeError, ValueError):
                    return self._json(400, {"error": f"tab_id not an integer: {tab_id_in!r}"})
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True})

        if self.path == "/panels/ctx":
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                return self._json(400, {"error": "usage must be an object"})
            now = time.time()
            with lock:
                diag = data.get("diag")
                if isinstance(diag, dict):
                    ctx_diag.clear()
                    ctx_diag.update(diag)
                    ctx_diag["seen"] = now
                for tid, u in usage.items():
                    if not isinstance(u, dict):
                        continue
                    ctx_usage[str(tid)] = {
                        "pct": u.get("pct"),
                        "input_tokens": u.get("input_tokens"),
                        "at": u.get("at"),
                        "seen": now,
                    }
            return self._json(200, {"stored": len(usage)})

        if self.path == "/panels/closing":
            # A bridge command is about to close this tab on purpose.
            try:
                tid = int(data.get("tab_id"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "tab_id must be an integer"})
            with lock:
                closing_intended.add(tid)
            return self._json(200, {"ok": True})

        if self.path == "/panels/meta/open":
            # open-panel --name: name the panel at open and start its 5 h
            # clock. The group is renamed once the extension push shows it.
            try:
                name = panel_meta.normalize_name(data.get("name") or "")
                tab_id = int(data.get("tab_id"))
            except (TypeError, ValueError) as e:
                return self._json(400, {"error": f"name and integer tab_id required: {e}"})
            # Identity at open, outside the lock (network): this Chrome launch
            # and this tab's side panel target (A3F3).
            browser = _cdp_browser_id()
            ptarget = panel_meta.panel_targets(_cdp_list() or [], CFC_EXT_ID).get(tab_id)
            with lock:
                entry = panel_meta_store.get(tab_id)
                if entry is None:
                    _drop_registered(tab_id)  # an auto-registered group title
                    name = panel_meta.unique_name(name, set(panels) | panel_meta_store.names())
                    now = time.time()
                    entry = {"tab_id": tab_id, "name": name, "opened_at": now,
                             "owner_pane": (data.get("owner_pane") or "").strip() or None,
                             "browser_id": browser, "panel_target": ptarget}
                    panels[name] = {"to": "browser", "tab_id": tab_id, "registered_at": now}
                    save_panels(panels)
                    panel_meta_store.put(entry)
            return self._json(200, {"ok": True, **entry})

        if self.path == "/popup/answer":
            # The user clicked a button on a request popup (bridge-notify --kind
            # request). One line into the asking pane, typed once its prompt
            # is free, exactly as the user asked for it.
            pane = str(data.get("pane") or "")
            button = " ".join(str(data.get("button") or "").split())[:60]
            question = " ".join(str(data.get("text") or "").split())[:300]
            auto = data.get("auto") is True
            if not re.fullmatch(r"%\d+", pane) or not (button or auto):
                return self._json(400, {"error": "pane (%N) and button required"})
            # auto: the popup's --done-when saw the action happen on the page.
            line = f"[popup] auto-done: {question}" if auto else f'[popup] user answered "{button}" to: {question}'

            def _answer():
                ok, detail = _paste_into_pane(pane, line, wait_ready=True, max_wait=REPLY_WAIT_S)
                print(f"[INFO] popup answer into {pane}: {detail}", file=sys.stderr)
            threading.Thread(target=_answer, daemon=True).start()
            return self._json(200, {"ok": True, "queued": pane})

        if self.path == "/panels/password-field":
            # The extension saw a visible password field in an idle agent's
            # own group (presence only). One sign-in popup per panel per
            # 10 min, shared with the agent's own "I can't sign in to" line.
            try:
                tab_id = int(data.get("tab_id"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "integer tab_id required"})
            cdp_live = cdp_sidepanel_tab_ids()
            with lock:
                tab_id, _ = _host_tab({"tab_id": tab_id, "tab_ids": data.get("tab_ids"),
                                       "group_id": data.get("group_id")}, _known_host_tabs(cdp_live))
                events = detector_state.on_password_field(tab_id, str(data.get("host") or ""), time.time())
            page = str(data.get("target_id") or "")
            if re.fullmatch(r"[0-9A-Fa-f]{32}", page):
                events = [(*e, page.upper()) for e in events]
            if events:
                threading.Thread(target=emit_events, args=(events,), daemon=True).start()
            return self._json(200, {"ok": True, "events": len(events)})

        if self.path == "/panels/meta/dismiss":
            # The user closed the "past 5 h and waiting for your OK" popup: the
            # next sweep deletes the panel even with the prompt still open.
            try:
                tab_id = int(data.get("tab_id"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "integer tab_id required"})
            with lock:
                if panel_meta_store.get(tab_id) is None:
                    return self._json(404, {"error": f"no panel-meta entry for tab {tab_id}"})
                panel_meta_store.update(tab_id, prompt_dismissed=True)
            return self._json(200, {"ok": True, "tab_id": tab_id})

        if self.path == "/panels/forget":
            # Remove a panel from panels.json. Used by the CLI's `forget`
            # subcommand AND by the extension's auto-cleanup on tab close.
            target = (data.get("target") or "").strip()
            tab_id_in = data.get("tab_id")
            if not target and tab_id_in is None:
                return self._json(400, {"error": "target or tab_id required"})
            with lock:
                old_name = None
                if target and target in panels:
                    old_name = target
                elif target:
                    pinfo = resolve_panel_name(target)
                    if pinfo and pinfo.get("kind") == "registered":
                        old_name = pinfo.get("name")
                if not old_name and tab_id_in is not None:
                    try:
                        tab_id_n = int(tab_id_in)
                    except (TypeError, ValueError):
                        return self._json(400, {"error": f"tab_id not an integer: {tab_id_in!r}"})
                    for n, p in panels.items():
                        if p.get("tab_id") == tab_id_n:
                            old_name = n
                            break
                if tab_id_in is not None:
                    try:
                        panel_meta_store.pop(int(tab_id_in))
                    except (TypeError, ValueError):
                        pass
                if not old_name:
                    return self._json(404, {
                        "error": f"no registered panel matches target={target!r} tab_id={tab_id_in!r}",
                        "hint": "list known panels with: browser-bridge list",
                    })
                entry = panels.pop(old_name)
                save_panels(panels)
                if entry.get("tab_id") is not None:
                    panel_meta_store.pop(entry["tab_id"])
            return self._json(200, {"forgotten": old_name, "tab_id": entry.get("tab_id")})

        if self.path == "/tabs/close":
            tab_ids = data.get("tab_ids") or []
            from_pane = (data.get("from_pane") or "%bridge").strip()
            if not isinstance(tab_ids, list) or not tab_ids:
                return self._json(400, {"error": "tab_ids must be a non-empty list"})
            cleaned: list[int] = []
            for t in tab_ids:
                try:
                    cleaned.append(int(t))
                except (TypeError, ValueError):
                    return self._json(400, {"error": f"tab_ids contains non-integer: {t!r}"})
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "tabs.close",
                "tab_ids": cleaned,
            }
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True, "tab_ids": cleaned})

        if self.path == "/browser/open-tab":
            # Operator typed `tmux-new-tab`. Open a fresh blank tab in the
            # bridge Chrome (no URL → default new-tab page). The operator then
            # clicks the Claude for Chrome extension icon to open the side
            # panel; the NEXT panel discovered via /panels/discovered (within
            # the 5-minute TTL) auto-gets relay_targets[new_tab_id] = from_pane
            # so its replies flow back to the originating terminal pane.
            from_pane_raw = data.get("from_pane")
            from_pane = (from_pane_raw or "").strip() if isinstance(from_pane_raw, str) else ""
            if not from_pane:
                return self._json(400, {"error": "from_pane required"})

            item = {
                "id": secrets.token_hex(8),
                "from_pane": from_pane,
                "mode": "control",
                "action": "tabs.create",
                "kind": "tabs.create",
                "want_sidepanel": False,
                "enqueued_at": time.time(),
            }
            # claim_only: the caller opens the tab itself (bridge-open-panel
            # presses Ctrl+T, Alt+W) and only needs the claim, so the panel is
            # bound to it and not announced or greeted from the last-active pane.
            claim_only = bool(data.get("claim_only"))
            with lock:
                if not claim_only:
                    outbox.append(item)
                pending_panel_claims.append({
                    "from_pane": from_pane,
                    "expires_at": time.time() + 300,  # 5 min TTL
                })

            return self._json(200, {"queued": not claim_only, "claimed": True, "id": item["id"]})

        if self.path == "/chrome/open-new-claude-tab":
            # Dashboard-facing: open a fresh tab in BRIDGE Chrome
            # deterministically. Two paths, tried in order:
            #   1. CDP HTTP `PUT /json/new` — talks directly to Chrome's
            #      remote-debugging port (9222). Whatever's listening on
            #      that port IS the bridge Chrome (only one Chrome can
            #      bind it), so the tab lands there regardless of how
            #      many extension instances are running.
            #   2. Fall back to enqueuing a tabs.create control item if
            #      CDP is unreachable. Less guaranteed but works when
            #      the bridge runs without Chrome's debugging port.
            # Body: {"url": "<override>"} optional. Default = the real
            # Chrome new-tab page (chrome://newtab/), not about:blank —
            # so the operator gets Google search, shortcuts, and the
            # Claude side panel (if it was previously opened in this
            # Chrome session).
            url = (data.get("url") or "chrome://newtab/").strip()
            if not (url.startswith("http://") or url.startswith("https://") or url.startswith("chrome://")):
                return self._json(400, {"error": "url must be http(s) or chrome://"})
            cdp_err = None
            try:
                host = _resolve_bridge_chrome_host()
                port = int(os.environ.get("CDP_PORT", "9222"))
                path = "/json/new?" + urllib.parse.quote(url, safe="")
                req = urllib.request.Request(
                    f"http://{host}:{port}{path}", method="PUT",
                )
                with urllib.request.urlopen(req, timeout=3) as r:
                    target = json.load(r)
                tid = target.get("id")
                # Activate (bring to front) the new tab.
                try:
                    urllib.request.urlopen(
                        f"http://{host}:{port}/json/activate/{tid}", timeout=2,
                    ).read()
                except Exception:
                    pass
                print(f"[INFO] /chrome/open-new-claude-tab via=cdp host={host} tid={tid} url={url or '(blank)'}", flush=True)
                return self._json(200, {
                    "ok": True, "via": "cdp", "target_id": tid,
                    "url": url or None,
                })
            except Exception as e:
                cdp_err = str(e)
            # Fallback: extension queue. NOTE: chrome:// URLs cannot
            # be passed to chrome.tabs.create via extension API in
            # most cases, so on this path we omit the URL — the
            # extension opens the default new-tab page (which is also
            # chrome://newtab/ functionally).
            item = {
                "id": secrets.token_hex(8),
                "from_pane": "dashboard",
                "mode": "control",
                "kind": "tabs.create",
                "want_sidepanel": False,
                "enqueued_at": time.time(),
            }
            if url and not url.startswith("chrome://"):
                item["url"] = url
            with lock:
                outbox.append(item)
            print(f"[INFO] /chrome/open-new-claude-tab via=extension url={url} (cdp failed: {cdp_err})", flush=True)
            return self._json(200, {
                "ok": True, "via": "extension", "id": item["id"],
                "url": url, "cdp_error": cdp_err,
            })

        if self.path == "/chrome/focus-tab":
            # Dashboard-facing: bring an existing Chrome tab to focus.
            # Body: {"tab_id": <int>}.
            try:
                tab_id = int(data.get("tab_id"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "tab_id required (int)"})
            item = {
                "id": secrets.token_hex(8),
                "from_pane": "dashboard",
                "mode": "control",
                "kind": "tabs.focus",
                "tab_id": tab_id,
                "enqueued_at": time.time(),
            }
            with lock:
                outbox.append(item)
            print(f"[INFO] /chrome/focus-tab tab_id={tab_id}", flush=True)
            return self._json(200, {"queued": True, "id": item["id"], "tab_id": tab_id})

        if self.path == "/browser/cancel-claim":
            # Clear pending_panel_claims entries for the caller's pane. Used
            # by `tmux-new-tab --cancel` when a previous open-tab call left
            # an orphan claim (operator closed the tab before creating a
            # chat). If from_pane is missing, clears ALL pending claims.
            from_pane_raw = data.get("from_pane")
            from_pane = (from_pane_raw or "").strip() if isinstance(from_pane_raw, str) else ""
            with lock:
                if from_pane:
                    before = len(pending_panel_claims)
                    kept = [c for c in pending_panel_claims if c.get("from_pane") != from_pane]
                    pending_panel_claims.clear()
                    pending_panel_claims.extend(kept)
                    cleared = before - len(pending_panel_claims)
                else:
                    cleared = len(pending_panel_claims)
                    pending_panel_claims.clear()
            return self._json(200, {"ok": True, "cleared": cleared, "from_pane": from_pane or None})

        if self.path == "/browser/rename-last-tab":
            # Rename the most-recent tab opened from a given pane via
            # tmux-new-tab. The pane→tab_id mapping is populated by the
            # auto-claim block in /panels/discovered, so this endpoint only
            # works after a successful tmux-new-tab + side-panel-open cycle.
            # When the caller omits `color`, the server auto-picks one from
            # the LRU palette so each tab is visually distinct.
            from_pane_raw = data.get("from_pane")
            from_pane = (from_pane_raw or "").strip() if isinstance(from_pane_raw, str) else ""
            new_name_raw = data.get("new_name")
            new_name = (new_name_raw or "").strip() if isinstance(new_name_raw, str) else ""
            color_raw = data.get("color")
            color = (color_raw or "").strip() if isinstance(color_raw, str) else None
            if not from_pane:
                return self._json(400, {"error": "from_pane required"})
            if not new_name:
                return self._json(400, {"error": "new_name required"})
            if not re.match(r"^[A-Za-z0-9_ -]{1,40}$", new_name):
                return self._json(400, {"error": "new_name must match ^[A-Za-z0-9_ -]{1,40}$"})
            if color and color not in ALLOWED_TAB_GROUP_COLORS:
                return self._json(400, {"error": f"color must be one of {list(ALLOWED_TAB_GROUP_COLORS)}"})

            with lock:
                info = last_tab_per_pane.get(from_pane)
                if not info:
                    color_chosen = None
                else:
                    tab_id = info["tab_id"]
                    if not color:
                        # Auto-pick a not-yet-used color (LRU fallback past 9 tabs).
                        color = _pick_unique_color()
                    tab_group_color[tab_id] = {"color": color, "assigned_at": time.time()}
                    outbox.append({
                        "id": secrets.token_hex(8),
                        "from_pane": from_pane,
                        "mode": "control",
                        "kind": "tabGroup.rename",
                        "tab_id": tab_id,
                        "to_title": new_name,
                        "color": color,
                    })
                    color_chosen = color
            if not info:
                return self._json(404, {"error": f"no last tab recorded for {from_pane}. Run tmux-new-tab first."})
            return self._json(200, {"ok": True, "tab_id": tab_id, "new_name": new_name, "color": color_chosen})

        if self.path == "/browser/close":
            from_pane = (data.get("from_pane") or "%bridge").strip()
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "windows.closeAll",
            }
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True})

        if self.path == "/tabgroups/list":
            from_pane = (data.get("from_pane") or "%bridge").strip()
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "tabGroup.list",
            }
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True})

        if self.path == "/bookmarks/search":
            query = (data.get("query") or "").strip()
            from_pane = (data.get("from_pane") or "%bridge").strip()
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "bookmark.search",
                "query": query,
            }
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True})

        if self.path == "/bookmarks/rename":
            from_title = (data.get("from") or "").strip()
            to_title = (data.get("to") or "").strip()
            from_pane = (data.get("from_pane") or "%bridge").strip()
            if not from_title or not to_title:
                return self._json(400, {"error": "from and to required"})
            item = {
                "id": uuid.uuid4().hex,
                "from_pane": from_pane,
                "mode": "control",
                "kind": "bookmark.rename",
                "from_title": from_title,
                "to_title": to_title,
            }
            with lock:
                outbox.append(item)
            return self._json(200, {"id": item["id"], "queued": True})

        if self.path == "/dispatch/streaming":
            mid = (data.get("id") or "").strip()
            if mid:
                with lock:
                    streaming_ids.add(mid)
                    rec = tasks.get(task_by_dispatch.get(mid, ""))
                    if rec and rec.get("dispatch_id") == mid and rec["state"] == "working":
                        rec["state"] = "streaming"
            return self._json(200, {"ok": True})

        if self.path == "/dispatch/progress":
            # Daemon's live read of the panel while a task message runs.
            mid = (data.get("id") or "").strip()
            with lock:
                rec = tasks.get(task_by_dispatch.get(mid, ""))
                if rec and rec.get("dispatch_id") == mid:
                    if isinstance(data.get("actions"), int):
                        rec["actions"] = data["actions"]
                    if data.get("last_action") is not None:
                        rec["last_action"] = str(data["last_action"])[:200]
            return self._json(200, {"ok": bool(rec)})

        if self.path == "/tasks/end":
            tid = str(data.get("task_id") or "").strip()
            st = data.get("state")
            if st not in ("done", "failed"):
                return self._json(400, {"error": "state must be done or failed"})
            with lock:
                rec = tasks.get(tid)
                if not rec:
                    return self._json(404, {"error": f"no task '{tid}'"})
                rec["state"] = st
                rec["ended_at"] = time.time()
            return self._json(200, {"ok": True, "task": rec})

        if self.path == "/panels/discovered":
            # Extension pushes its current view of tab groups + their tabs.
            # Each item: {group_title: str, tab_id: int, group_id: int, tab_title: str}
            items = data.get("items") or []
            if not isinstance(items, list):
                return self._json(400, {"error": "items must be a list"})
            # Asked before taking the lock: this can hit Chrome over the
            # network (cached 3 s) and the lock is on the push hot path.
            cdp_live = cdp_sidepanel_tab_ids()
            with lock:
                known_hosts = _known_host_tabs(cdp_live)
            new_state = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = (it.get("group_title") or "").strip()
                if not title or it.get("tab_id") is None:
                    continue
                tab_id, target_id = _host_tab(it, known_hosts)
                # Chrome happily allows several tab groups with the SAME
                # title. This dict is keyed by title, so a duplicate used to
                # overwrite the earlier group and only one of them survived
                # to the panel list. Key on the group id when a title
                # repeats, and carry the real title in the value so display
                # names are unaffected.
                key = title if title not in new_state else f"{title}#{it.get('group_id')}"
                new_state[key] = {
                    "tab_id": int(tab_id),
                    "group_id": it.get("group_id"),
                    "tab_title": (it.get("tab_title") or "")[:160],
                    "group_color": it.get("group_color") or None,
                    "group_collapsed": bool(it.get("group_collapsed")),
                    "group_title": title,
                    "target_id": target_id,
                }
            with lock:
                # Record the LIVE Chrome tab-group colour for each tab. The
                # extension is the only thing that can see chrome.tabGroups,
                # so this push is how the colour reaches surfaces that can't
                # (the localhost dashboard). Entries here are overwritten on
                # every push, so a regrouped tab re-colours on the next poll.
                for v in new_state.values():
                    c = v.get("group_color")
                    if c in ALLOWED_TAB_GROUP_COLORS:
                        tab_group_color[v["tab_id"]] = {
                            "color": c, "assigned_at": time.time(),
                        }
                before_ids = {v["tab_id"] for v in discovered_panels.values()}
                # MERGE, do not replace. One extension instance runs per Chrome
                # profile and each pushes only the panels IT can see, so a
                # wholesale replace made every other profile's panels vanish and
                # reappear every second push: the panel list flapped, /brN
                # aliases pointed at a different tab from one call to the next,
                # and registered panels were "forgotten" and re-announced.
                # Entries carry last_seen and are evicted only after a real
                # absence, since every extension re-pushes every few seconds.
                seen_now = time.time()
                pushed_tabs = {v["tab_id"] for v in new_state.values()}
                for k in [k for k, v in discovered_panels.items()
                          if v.get("tab_id") in pushed_tabs]:
                    discovered_panels.pop(k, None)
                for k, v in new_state.items():
                    v["last_seen"] = seen_now
                    discovered_panels[k] = v
                for k in [k for k, v in discovered_panels.items()
                          if seen_now - v.get("last_seen", seen_now) > PANEL_GRACE_S]:
                    discovered_panels.pop(k, None)
                after_ids = {v["tab_id"] for v in discovered_panels.values()}
                push_events = detector_state.on_push(list(new_state.values()), seen_now)

                # Panels opened with --name: give the group our name and a
                # color once it exists, keeping Claude for Chrome's prefix.
                for v in new_state.values():
                    e = panel_meta_store.get(v["tab_id"])
                    if e and not e.get("orphan_since"):
                        # First sighting fixes the group and CDP target this
                        # panel lives in (A3F3, A3F4); later changes are not
                        # copied over.
                        first = {k: v[k] for k in ("group_id", "target_id")
                                 if v.get(k) is not None and e.get(k) is None}
                        if first:
                            panel_meta_store.update(v["tab_id"], **first)
                    title = e and panel_meta.rename_needed(e, v.get("group_title") or "", seen_now)
                    if not title:
                        continue
                    color = e.get("color")
                    if not color:
                        color = panel_meta.pick_color(
                            [d.get("group_color") for d in discovered_panels.values()
                             if d.get("tab_id") != v["tab_id"]], our_color_picks)
                        our_color_picks[color] = seen_now
                    panel_meta_store.update(v["tab_id"], renamed_at=seen_now, color=color)
                    outbox.append({
                        "id": secrets.token_hex(8),
                        "from_pane": "%bridge",
                        "mode": "control",
                        "kind": "tabGroup.rename",
                        "tab_id": v["tab_id"],
                        "to_title": title,
                        "color": color,
                    })

                # A context reading belongs to a live chat. When the panel's
                # tab is gone the number is a ghost, and a surface that shows
                # it reports a chat that no longer exists. This push is the
                # extension's live view of Chrome, so it is the moment to
                # drop readings for tabs that are no longer open.
                for gone in [t for t in ctx_usage if int(t) not in after_ids]:
                    ctx_usage.pop(gone, None)

                # Drop expired pending claims.
                now = time.time()
                while pending_panel_claims and pending_panel_claims[0]["expires_at"] < now:
                    pending_panel_claims.popleft()
                # Also sweep any expired entries deeper in the deque (rare —
                # claims usually arrive in order, but be safe).
                live = [c for c in pending_panel_claims if c["expires_at"] >= now]
                pending_panel_claims.clear()
                pending_panel_claims.extend(live)

                # Auto-claim: if any tab_ids appeared in this push that
                # weren't there before, the oldest unexpired claim is bound
                # to the smallest new tab_id. Only one claim consumed per push.
                new_tab_ids = sorted(after_ids - before_ids)
                claimed_pane = None
                claimed_tab_id = None
                claimed_panel_name = None
                # Not on the first push after a start: every panel already
                # open looks new then, and one took a claim meant for a panel
                # opened a moment later (2026-10-04, after a server restart).
                if new_tab_ids and pending_panel_claims and discovery_state["seeded"]:
                    claim = pending_panel_claims.popleft()
                    claimed_tab_id = new_tab_ids[0]
                    claimed_pane = claim["from_pane"]
                    relay_targets[claimed_tab_id] = claimed_pane
                    last_tab_per_pane[claimed_pane] = {"tab_id": claimed_tab_id, "opened_at": time.time()}
                    for name, meta in discovered_panels.items():
                        if meta.get("tab_id") == claimed_tab_id:
                            claimed_panel_name = meta.get("group_title") or name
                            break

                # Announce panels that opened on their own. A claimed panel is
                # skipped: the pane that claimed it already knows it asked for
                # it, so a confirmation would be noise.
                to_announce = []
                if not discovery_state["seeded"]:
                    for tid in after_ids:
                        announced_at[tid] = now
                    discovery_state["seeded"] = True
                else:
                    for tid in new_tab_ids:
                        if tid == claimed_tab_id:
                            continue
                        # A tab never announced before is genuinely new, and the
                        # operator is sitting there waiting for the confirmation.
                        # Send it immediately: the throttles below exist to stop
                        # RE-announcement storms, and making a first connect wait
                        # behind another tab's cooldown cost up to ANNOUNCE_MIN_GAP_S
                        # of dead time at the one moment latency is visible.
                        if tid not in announced_at:
                            announced_at[tid] = now
                            discovery_state["last_announce"] = now
                            nm = next((m.get("group_title") or nn
                                       for nn, m in discovered_panels.items()
                                       if m.get("tab_id") == tid), str(tid))
                            to_announce.append((tid, nm))
                            continue
                        # Known tab. It only re-qualifies as "new" because each
                        # push replaces discovered_panels wholesale and tabs flip
                        # in and out. Require a real absence before announcing it
                        # again, not merely a gap in one push.
                        gone_since = panel_missing_since.get(tid)
                        if not gone_since or (now - gone_since) < PANEL_GRACE_S:
                            continue
                        # Per-tab cooldown, not a one-shot set. More than one
                        # extension can push here (one per Chrome window), and
                        # each push replaces discovered_panels wholesale, so
                        # tabs flip in and out of `new_tab_ids` continuously
                        # and a set that forgets on absence re-announces them
                        # forever. This was a live announcement storm.
                        if now - announced_at.get(tid, 0) < ANNOUNCE_COOLDOWN_S:
                            continue
                        # Global throttle. Whatever new way this manages to
                        # misfire, it can never flood the operator's pane.
                        if now - discovery_state["last_announce"] < ANNOUNCE_MIN_GAP_S:
                            continue
                        announced_at[tid] = now
                        discovery_state["last_announce"] = now
                        nm = next((m.get("group_title") or n
                                   for n, m in discovered_panels.items()
                                   if m.get("tab_id") == tid), str(tid))
                        to_announce.append((tid, nm))
                # Prune entries far older than the cooldown so the dict cannot
                # grow without bound across a long-lived server.
                for tid in [t for t, ts in announced_at.items()
                            if now - ts > ANNOUNCE_COOLDOWN_S * 10]:
                    announced_at.pop(tid, None)

                # Forget registered panels whose tab has been gone for a while.
                # A dead row is worse than no row: dispatches to it fail, and it
                # occupies a slot in the positional /brN numbering, shifting the
                # aliases of every panel after it. Requires sustained absence
                # rather than one missing push, because the extension briefly
                # omits a tab while Chrome is regrouping it.
                dropped = []
                for pname, pmeta in list(panels.items()):
                    ptab = pmeta.get("tab_id")
                    if ptab is None:
                        continue
                    # Absent from THIS push is not gone. Every Chrome window's
                    # extension pushes its own view and each push replaces the
                    # map wholesale, so a panel in another window looks dead
                    # every second push. Chrome itself is the authority: a tab
                    # whose side panel is open over CDP is alive.
                    if int(ptab) in after_ids or int(ptab) in cdp_live:
                        panel_missing_since.pop(int(ptab), None)
                        continue
                    first_missed = panel_missing_since.setdefault(int(ptab), now)
                    if now - first_missed > PANEL_GRACE_S:
                        panels.pop(pname, None)
                        panel_missing_since.pop(int(ptab), None)
                        dropped.append((pname, ptab))
                if dropped:
                    save_panels(panels)

            for pname, ptab in dropped:
                print(f"[INFO] forgot dead panel {pname}(tab_id={ptab}) after "
                      f"{PANEL_GRACE_S}s absent from discovery", file=sys.stderr)

            for tid, nm in to_announce:
                threading.Thread(target=announce_new_panel, args=(tid, nm),
                                 daemon=True).start()

            if claimed_pane is not None:
                save_relay_state()
                print(
                    f"[INFO] auto-claimed new panel {claimed_panel_name}(tab_id={claimed_tab_id}) → relay_target={claimed_pane}",
                    file=sys.stderr,
                )
            if push_events:
                threading.Thread(target=emit_events, args=(push_events,), daemon=True).start()
            return self._json(200, {"received": len(new_state)})

        if self.path == "/tabs":
            tabs = data.get("tabs") or []
            if not isinstance(tabs, list):
                return self._json(400, {"error": "tabs must be a list"})
            cleaned = []
            for t in tabs:
                if not isinstance(t, dict):
                    continue
                cleaned.append({
                    "id": t.get("id"),
                    "title": t.get("title", ""),
                    "url": t.get("url", ""),
                    "active": bool(t.get("active")),
                    "windowId": t.get("windowId"),
                })
            with lock:
                tabs_state["tabs"] = cleaned
                tabs_state["updated_at"] = time.time()
            return self._json(200, {"received": len(cleaned)})

        if self.path.startswith("/panels/") and self.path.endswith("/rename"):
            from urllib.parse import unquote
            inner = self.path[len("/panels/"):-len("/rename")]
            target = unquote(inner)
            new_name = (data.get("new_name") or "").strip()
            if not new_name:
                return self._json(400, {"error": "new_name required"})
            if not new_name.replace("-", "").replace("_", "").isalnum():
                return self._json(400, {"error": "new_name must be alnum/dash/underscore"})
            with lock:
                old_name = None
                if target in panels:
                    old_name = target
                else:
                    pinfo = resolve_panel_name(target)
                    if pinfo and pinfo.get("kind") == "registered":
                        old_name = pinfo.get("name")
                if not old_name or old_name not in panels:
                    # Distinguish "discovered but not registered" from "unknown
                    # entirely" so the operator knows whether to send a ping
                    # (which auto-registers on first reply) or check the name.
                    pinfo = resolve_panel_name(target)
                    if pinfo and pinfo.get("kind") == "discovered":
                        return self._json(404, {
                            "error": f"panel '{target}' is discovered but not yet registered",
                            "kind": "discovered",
                            "hint": "send any message to this panel first — the bridge auto-promotes it to a registered panel on the first successful reply, then retry name.",
                            "tab_id": pinfo.get("tab_id"),
                        })
                    return self._json(404, {
                        "error": f"unknown registered panel: {target}",
                        "hint": "list known panels with: browser-bridge list",
                    })
                if new_name in panels and new_name != old_name:
                    return self._json(400, {"error": f"name '{new_name}' already in use"})
                entry = panels.pop(old_name)
                panels[new_name] = entry
                save_panels(panels)
                tab_id = entry.get("tab_id")
                # A panel opened with --name keeps its name in panel-meta too;
                # without this the next push renamed the group back (A3F6).
                if tab_id is not None and panel_meta_store.get(tab_id):
                    panel_meta_store.update(tab_id, name=new_name, renamed_at=time.time())
                # Also rename the Chrome tab group so the popup, the
                # browser tab strip, and panels.json all stay in sync.
                # Extension's background.js handles kind=tabGroup.rename
                # via chrome.tabGroups.update. Color is left unchanged
                # (omit field -> extension preserves current color).
                if tab_id is not None:
                    outbox.append({
                        "id": secrets.token_hex(8),
                        "from_pane": "%bridge",
                        "mode": "control",
                        "kind": "tabGroup.rename",
                        "tab_id": int(tab_id),
                        "to_title": new_name,
                    })
            return self._json(200, {
                "ok": True,
                "old_name": old_name,
                "new_name": new_name,
                "tab_id": tab_id,
            })

        return self._json(404, {"error": "not found"})


def main():
    bind = os.environ.get("CLAUDE_BRIDGE_BIND", "127.0.0.1")
    server = ThreadingHTTPServer((bind, PORT), Handler)
    threading.Thread(target=panel_close_watcher, daemon=True).start()
    threading.Thread(target=panel_sweeper, daemon=True).start()
    threading.Thread(target=signed_out_watcher, daemon=True).start()
    threading.Thread(target=prompt_watcher, daemon=True).start()
    print(f"browser-claude-bridge listening on http://{bind}:{PORT}", file=sys.stderr)
    # Don't log the secret. sha8 prefix is enough to compare against the
    # extension popup at-a-glance without leaving the value in log files.
    print(f"token-file: {TOKEN_FILE} (sha8={_token_sha8(TOKEN)})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
