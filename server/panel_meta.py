"""Panel names, group colors and the 5 h cleanup rule (agent-system plan A3).

Pure logic plus the panel-meta.json store, so it can be tested without Chrome
or the server. server.py owns the threads, the lock and the side effects.

panel-meta.json: {"<tab_id>": {tab_id, name, owner_pane, opened_at,
chat_id?, target_id?, browser_id?, panel_target?, color?, renamed_at?, deferrals?, prompt_notified_at?,
prompt_dismissed?, orphan_since?}}. It holds when and under what name a panel
was opened, never what the agent did (patch note #21).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Claude for Chrome 1.0.102 keeps one of these at the start of its group
# title: working, permission prompt waiting, done (updateTabGroupPrefix).
STATUS_PREFIXES = ("⌛", "\U0001f514", "✅")  # ⌛ 🔔 ✅
WORKING, PROMPT, DONE = STATUS_PREFIXES

# Chrome's palette in the order Claude for Chrome picks from it.
PALETTE = ("grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange")

NAME_MAX = 30
TTL_S = 5 * 3600


def split_prefix(title: str) -> tuple[str, str]:
    """("⌛", "name") for "⌛name". Prefix is "" when there is none."""
    t = (title or "").strip()
    for p in STATUS_PREFIXES:
        if t.startswith(p):
            return p, t[len(p):].strip()
    return "", t


def normalize_name(task: str) -> str:
    """A task name as a group title: lowercase words joined by "-", at most
    NAME_MAX chars, only [a-z0-9-] (what /panels/<x>/rename accepts)."""
    words = re.findall(r"[a-z0-9]+", (task or "").lower())
    name = "-".join(words)[:NAME_MAX].strip("-")
    if not name:
        raise ValueError(f"no usable name in {task!r}")
    return name


def unique_name(name: str, taken) -> str:
    """name, or name-2, name-3, ... if taken. Stays within NAME_MAX."""
    taken = set(taken)
    if name not in taken:
        return name
    n = 2
    while True:
        suffix = f"-{n}"
        cand = name[:NAME_MAX - len(suffix)].rstrip("-") + suffix
        if cand not in taken:
            return cand
        n += 1


def pick_color(live_colors, last_used: dict | None = None) -> str:
    """The color fewest open groups use. Ties go to the color we picked
    longest ago, then palette order, so unused colors come first, in the same
    order Claude for Chrome uses."""
    last_used = last_used or {}
    counts = {c: 0 for c in PALETTE}
    for c in live_colors:
        if c in counts:
            counts[c] += 1
    return min(PALETTE, key=lambda c: (counts[c], last_used.get(c, 0.0), PALETTE.index(c)))


def rename_needed(entry: dict, group_title: str, now: float, min_gap: float = 60.0) -> str | None:
    """The title to set, or None. Keeps Claude for Chrome's status prefix.
    Re-applies at most once per min_gap (Claude for Chrome may re-create the
    group as "Claude" when the main tab moves)."""
    prefix, bare = split_prefix(group_title)
    if bare == entry["name"]:
        return None
    if now - entry.get("renamed_at", 0.0) < min_gap:
        return None
    return prefix + entry["name"]


# Sweep decisions.
KEEP = "keep"              # not due yet
BUSY = "busy"              # due, but replying or queued: retry next sweep
NOTIFY_PROMPT = "notify"   # due with a prompt waiting: one sticky popup
HOLD_PROMPT = "hold"       # popup already shown, prompt still waiting
DELETE = "delete"
ORPHAN = "orphan"          # tab gone (Chrome restart): list, never open a panel


def same_panel(entry: dict, live_chat: str | None, live_target: str | None,
               live_browser: str | None = None, live_panel_target: str | None = None) -> bool:
    """A tab id alone is not an identity: Chrome reuses ids after a restart
    and a panel can start a new chat. Once the entry knows its Chrome
    session, side panel target, chat or tab target, a different live one
    means a different panel (A3F3). The browser session and panel target come
    from the same fresh CDP read as the chat, so a stale extension push
    cannot vouch for a panel from before a restart."""
    if entry.get("browser_id") and live_browser and live_browser != entry["browser_id"]:
        return False
    if entry.get("panel_target") and live_panel_target and live_panel_target != entry["panel_target"]:
        return False
    if entry.get("chat_id") and live_chat and live_chat != entry["chat_id"]:
        return False
    if entry.get("target_id") and live_target and live_target != entry["target_id"]:
        return False
    if entry.get("orphan_since"):
        # Lost once: it is ours again only if its own chat is back.
        return bool(entry.get("chat_id")) and live_chat == entry["chat_id"]
    return True


def decide(entry: dict, now: float, *, ttl: float, tab_live: bool,
           title_prefix: str, busy: bool, live_chat: str | None = None,
           live_target: str | None = None, live_browser: str | None = None,
           live_panel_target: str | None = None) -> str:
    """What the sweep does with one panel. D-TTL (The user, 2026-10-25): the
    clock starts at open; a panel mid-reply or with a prompt waiting is
    deleted right after it finishes."""
    if not tab_live or not same_panel(entry, live_chat, live_target, live_browser, live_panel_target):
        return ORPHAN
    if now - entry["opened_at"] < ttl:
        return KEEP
    # A panel waiting on a permission prompt is also "in flight"; the prompt
    # decides first, so the user hears about it instead of the panel waiting forever.
    if title_prefix == PROMPT and not entry.get("prompt_dismissed"):
        return HOLD_PROMPT if entry.get("prompt_notified_at") else NOTIFY_PROMPT
    if busy or title_prefix == WORKING:
        return BUSY
    return DELETE


def panel_targets(targets: list[dict], ext_id: str) -> dict[int, str]:
    """tab_id -> the side panel's own CDP target id. A new panel in the same
    tab (or after a Chrome restart) has a new one."""
    out = {}
    for t in targets:
        m = re.match(rf"chrome-extension://{ext_id}/sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
        if m:
            out[int(m.group(1))] = t.get("id")
    return out


def chat_ids(targets: list[dict], ext_id: str) -> dict[int, str]:
    """tab_id -> cse_ chat id for every side panel whose claude.ai frame
    shows a task, from one CDP /json/list."""
    panels = {}
    for t in targets:
        m = re.match(rf"chrome-extension://{ext_id}/sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
        if m:
            panels[t.get("id")] = int(m.group(1))
    out = {}
    for t in targets:
        tab = panels.get(t.get("parentId"))
        m = re.search(r"/task/(cse_[A-Za-z0-9]+)", t.get("url", ""))
        if tab is not None and m:
            out[tab] = m.group(1)
    return out


class MetaStore:
    """panel-meta.json, keyed by tab id as a string. Not thread-safe: the
    server calls it under its lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, indent=2))
        os.replace(tmp, self.path)

    def get(self, tab_id) -> dict | None:
        return self.d.get(str(tab_id))

    def put(self, entry: dict) -> None:
        self.d[str(entry["tab_id"])] = entry
        self.save()

    def update(self, tab_id, **fields) -> None:
        e = self.d.get(str(tab_id))
        if e is not None:
            e.update(fields)
            self.save()

    def pop(self, tab_id) -> dict | None:
        e = self.d.pop(str(tab_id), None)
        if e is not None:
            self.save()
        return e

    def entries(self) -> list[dict]:
        return list(self.d.values())

    def names(self) -> set[str]:
        return {e["name"] for e in self.d.values()}
