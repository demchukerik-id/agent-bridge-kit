"""Event detectors for browser agents (agent-system plan A2, research RQ5).

Pure state machines: the server feeds them the extension's group push, the
CDP target list and finished replies, and turns the returned events into a
popup (notify_user) and a line in the owning pane. Nothing here talks to
Chrome, so it is tested without one.

Events: (kind, tab_id or None, detail). Kinds:
  prompt      a permission prompt has waited PROMPT_HOLD_S (🔔 on the group)
  signed_out  the Claude extension shows its sign-in screen
  login       the agent wrote "I can't sign in to <site>", or its idle
              group shows a password field (extension, login_watch.js)
  stopped     the agent wrote "I stopped because <what>"
"""
from __future__ import annotations

import re

from panel_meta import PROMPT, split_prefix

PROMPT_HOLD_S = 5.0      # the user answering within this makes no popup
PROMPT_MIN_PUSHES = 2    # and the 🔔 must be seen in two pushes (3 s apart)
NO_FRAME_S = 5.0         # a new panel has no claude.ai frame for a second or two
LOGIN_DEDUP_S = 600.0    # one sign-in popup per panel in this window, whichever signal came first
RECHECK_S = 60.0         # a frameless panel that was not signed out is read again after this

# Claude for Chrome 1.0.102 sign-in screen texts (i18n ahcT/xfBaf,
# 2ZM4JNJI7K, odXlk858Gb, svPMOhpSDs; research/RQ5 F5).
SIGNED_OUT_TEXTS = ("Sign in again to continue using Claude in Chrome",
                    "Your session has expired",
                    "Already signed in? Check that the Claude extension has access")
SIGNED_OUT_JS = "document.body ? document.body.innerText : ''"

# The phrases the oversight skill asks the agent to use (PLAN A5).
LOGIN_RE = re.compile(r"\bI\s+(?:can't|can’t|cannot|can not|couldn't|couldn’t)\s+"
                      r"(?:sign|log)\s*in(?:to|\s+to)?\s+(?P<site>.+?)(?=[,;:!?\n]|\.(?:\s|$)|$)", re.I)
# Words that end the site name: "I can't sign in to Example right now".
SITE_END_RE = re.compile(r"\s+(?:because|right|now|with|using|so|and|but|until|without|as|on|for)\b.*", re.I)
STOPPED_RE = re.compile(r"\bI\s+stopped\s+because\s+(?P<what>[^\n]+)", re.I)
DETAIL_MAX = 60


def is_signed_out(text: str) -> bool:
    return any(s.lower() in (text or "").lower() for s in SIGNED_OUT_TEXTS)


def _short(s: str) -> str:
    s = re.split(r"(?<=[.!?])\s", s.strip(), maxsplit=1)[0].strip().rstrip(".!?").strip()
    return s if len(s) <= DETAIL_MAX else s[:DETAIL_MAX - 1].rstrip() + "…"


def match_reply(text: str) -> list[tuple[str, str]]:
    """[(kind, detail)] for the two plain sentences in a finished reply."""
    out = []
    m = LOGIN_RE.search(text or "")
    if m:
        out.append(("login", _short(SITE_END_RE.sub("", m.group("site")))))
    m = STOPPED_RE.search(text or "")
    if m:
        out.append(("stopped", _short(m.group("what"))))
    return out


def message(kind: str, name: str, detail: str = "") -> tuple[str, bool]:
    """(popup text, sticky). Short enough to read at a glance."""
    if kind == "prompt":
        return f"{name} is waiting for your OK.", False
    if kind == "signed_out":
        return "Claude extension is signed out.", False
    if kind == "login":
        return f"{name} needs you to sign in to {detail}.", False
    if kind == "stopped":
        return f"{name} stopped: {detail}.", True
    raise ValueError(kind)


class Detectors:
    """Not thread-safe: the server calls it under its lock."""

    def __init__(self):
        self.prompt_first: dict[int, float] = {}   # tab -> when 🔔 was first seen
        self.prompt_pushes: dict[int, int] = {}
        self.prompt_fired: set[int] = set()
        self.no_frame_since: dict[int, float] = {}
        self.checked: dict[int, float] = {}        # no-frame tab -> when it was last evaluated
        self.signed_out: set[int] = set()
        self.login_at: dict[int, float] = {}       # tab -> last sign-in popup

    def on_push(self, items, now: float) -> list:
        """items: [{tab_id, group_title}] from one extension push. A push is
        one profile's view, so a tab missing from it changes nothing."""
        events = []
        for it in items:
            tab = it["tab_id"]
            prefix, _ = split_prefix(it.get("group_title") or "")
            if prefix != PROMPT:
                self.prompt_first.pop(tab, None)
                self.prompt_pushes.pop(tab, None)
                self.prompt_fired.discard(tab)
                continue
            self.prompt_first.setdefault(tab, now)
            self.prompt_pushes[tab] = self.prompt_pushes.get(tab, 0) + 1
            if (tab not in self.prompt_fired and self.prompt_pushes[tab] >= PROMPT_MIN_PUSHES
                    and now - self.prompt_first[tab] >= PROMPT_HOLD_S):
                self.prompt_fired.add(tab)
                events.append(("prompt", tab, ""))
        return events

    def frames_to_check(self, targets, ext_id: str, now: float) -> list[dict]:
        """Side panel targets that have had no claude.ai child frame for
        NO_FRAME_S and were not read in the last RECHECK_S. The caller evaluates
        SIGNED_OUT_JS in each and passes the text to on_panel_text."""
        panels, framed = {}, set()
        for t in targets:
            m = re.match(rf"chrome-extension://{ext_id}/sidepanel\.html\?tabId=(\d+)", t.get("url", ""))
            if m:
                panels[t.get("id")] = (int(m.group(1)), t)
        for t in targets:
            if t.get("parentId") in panels and t.get("url", "").startswith("https://claude.ai/"):
                framed.add(panels[t["parentId"]][0])
        live = {tab for tab, _ in panels.values()}
        for tab in list(self.no_frame_since):
            if tab in framed or tab not in live:
                self.no_frame_since.pop(tab)
                self.checked.pop(tab, None)
                self.signed_out.discard(tab)
        out = []
        for tab, t in panels.values():
            if tab in framed:
                continue
            since = self.no_frame_since.setdefault(tab, now)
            if (now - since >= NO_FRAME_S and tab not in self.signed_out
                    and now - self.checked.get(tab, float("-inf")) >= RECHECK_S):
                self.checked[tab] = now
                out.append(t)
        return out

    def on_panel_text(self, tab: int, text: str) -> list:
        """One popup while any panel shows the sign-in screen: signing out
        hits every panel of the profile at once."""
        if not is_signed_out(text):
            return []
        first = not self.signed_out
        self.signed_out.add(tab)
        return [("signed_out", None, "")] if first else []

    def _login(self, tab: int, site: str, now: float) -> list:
        if now - self.login_at.get(tab, float("-inf")) < LOGIN_DEDUP_S:
            return []
        self.login_at[tab] = now
        return [("login", tab, site)]

    def on_reply(self, tab: int, text: str, now: float) -> list:
        """Every match in a finished reply. A login line is not deduplicated
        here: the server answers it with bridge-login (A8) and calls
        login_popup() only for a popup."""
        return [(kind, tab, detail) for kind, detail in match_reply(text)]

    def login_popup(self, tab: int, site: str, now: float) -> list:
        """A sign-in popup for this panel, unless one was shown or a login
        was handled within LOGIN_DEDUP_S. Records the time either way."""
        return self._login(tab, site, now)

    def on_password_field(self, tab: int, host: str, now: float) -> list:
        """The extension found a visible password field in the idle agent's
        own group. The site shown is the page's host name."""
        host = (host or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return self._login(tab, _short(host) or "a site", now)
