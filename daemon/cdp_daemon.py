#!/usr/bin/env python3
"""
cdp-daemon — bridge consumer for sidepanel mode.

Polls the bridge server's queue with mode=sidepanel, drives the Claude Chrome
extension's side panel via Chrome's DevTools Protocol on :9222, and posts
replies back to the bridge.

Prereq: Chrome must have been launched with --remote-debugging-port=9222.

Subcommands:
  cdp_daemon.py discover            attach to side panel and dump DOM hints
  cdp_daemon.py run                 main poll loop
  cdp_daemon.py targets             print /json/list summary
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Allow importing cdp.py from same dir
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cdp import CDPClient, list_targets, get_version  # type: ignore

CLAUDE_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
BRIDGE_URL = os.environ.get("CLAUDE_BRIDGE_URL", "http://127.0.0.1:7777")
# How many sidepanel dispatches may run CONCURRENTLY across different tabs.
# Each agent (browser panel) gets its own worker thread + CDP connection, so
# while agent A is mid-task agent B can already be typed-into and running.
# Dispatches to the SAME tab still serialize (per-tab lock) to avoid clobber.
CONCURRENCY = max(1, int(os.environ.get("CLAUDE_BRIDGE_CONCURRENCY", "3")))
TOKEN_FILE = Path.home() / ".config" / "claude-web-bridge" / "token"


def _detect_windows_host() -> str | None:
    """In WSL2 the Windows host is the default gateway. resolv.conf's nameserver
    is only a second opinion: with DNS tunnelling it can point elsewhere."""
    try:
        import subprocess
        r = subprocess.run(["sh", "-c", "ip route show default | awk '{print $3}'"],
                           capture_output=True, text=True, timeout=3)
        gw = r.stdout.strip().split()[0] if r.stdout.strip() else ""
        if gw:
            return gw
    except Exception:
        pass
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1]
    except Exception:
        pass
    return None


def _in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


def _candidate_hosts() -> list[str]:
    """Explicit CDP_HOST wins. In WSL the Windows host comes first and
    127.0.0.1 last: 127.0.0.1:9334 here is not Chrome, and on 2026-09-19 it
    answered with a target list that had no side panel, so a daemon that tried
    it first reported every panel as missing. On Windows itself 127.0.0.1 is
    the right answer, so it stays in the list."""
    explicit = os.environ.get("CDP_HOST")
    if explicit:
        return [explicit]
    out: list[str] = []
    win = _detect_windows_host()
    if _in_wsl():
        if win:
            out.append(win)
        out.append("127.0.0.1")
    else:
        out.append("127.0.0.1")
        if win:
            out.append(win)
    # resolv.conf nameserver as a last resort if it differs from the gateway
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                if line.strip().startswith("nameserver"):
                    ns = line.split()[1]
                    if ns not in out:
                        out.append(ns)
                    break
    except Exception:
        pass
    return out


_WORKING_HOST: str | None = None  # remembered once a host shows a side panel


def _resolve_cdp_host() -> str:
    return _candidate_hosts()[0]


def _try_hosts() -> list[str]:
    hosts = _candidate_hosts()
    if _WORKING_HOST and _WORKING_HOST in hosts:
        hosts.remove(_WORKING_HOST)
        hosts.insert(0, _WORKING_HOST)
    return hosts


DEBUG_HOST = _resolve_cdp_host()
# 9334, not 9222: svchost.exe holds 0.0.0.0:9222 on this machine, so a
# daemon defaulting to 9222 connects to nothing and reports no panels.
# Chrome binds 127.0.0.1:9333 and a Windows forwarder fronts 9334.
DEBUG_PORT = int(os.environ.get("CDP_PORT", "9334"))
POLL_INTERVAL_S = 0.2  # how often daemon checks server outbox; lower = faster pickup
POLL_BACKOFF_S = 5.0


def log(*args):
    print("[cdp-daemon]", *args, file=sys.stderr, flush=True)


def token() -> str:
    return TOKEN_FILE.read_text().strip()


def bridge(method: str, path: str, body: dict | None = None, mode: str | None = None) -> tuple[int, dict | None]:
    url = BRIDGE_URL + path
    if mode and "?" not in path:
        url += f"?mode={mode}"
    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if not raw:
                return r.status, None
            return r.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: payload = json.loads(e.read().decode("utf-8"))
        except Exception: payload = None
        return e.code, payload


def _list_targets_resilient() -> tuple[str, list[dict]]:
    """Return the first host whose target list contains a Claude side panel.
    A host that answers but shows no panel is kept only as a fallback, so a
    stray listener on 127.0.0.1 cannot shadow the real Chrome (2026-09-19)."""
    global _WORKING_HOST
    last_err: Exception | None = None
    answered: tuple[str, list[dict]] | None = None
    for host in _try_hosts():
        try:
            targets = list_targets(host, DEBUG_PORT)
        except Exception as e:
            last_err = e
            continue
        has_panel = any(t.get("url", "").startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/")
                        and "sidepanel" in t.get("url", "").lower() for t in targets)
        if has_panel:
            if _WORKING_HOST != host:
                log(f"CDP host: {host}:{DEBUG_PORT} has the Claude side panel; using it")
                _WORKING_HOST = host
            return host, targets
        if answered is None:
            answered = (host, targets)
    if answered is not None:
        return answered
    raise ConnectionError(
        f"cannot reach Chrome on any of {_try_hosts()}:{DEBUG_PORT} — "
        f"was Chrome launched with --remote-debugging-port=9222? "
        f"From WSL, may also need --remote-debugging-address=0.0.0.0. "
        f"Last error: {last_err}"
    )


def find_sidepanel_target(tab_id: int | None = None) -> tuple[str, dict | None]:
    """Find a Claude side panel target. If tab_id given, return the panel whose
    URL has ?tabId=<tab_id>. Otherwise return any side panel (first found)."""
    host, targets = _list_targets_resilient()
    panels = [t for t in targets if
              t.get("url", "").startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/")
              and "sidepanel" in t.get("url", "").lower()]
    if tab_id is None:
        return host, (panels[0] if panels else None)
    for t in panels:
        # URL looks like .../sidepanel.html?tabId=1531859270
        if f"tabId={tab_id}" in t.get("url", ""):
            return host, t
    return host, None


def list_sidepanels() -> tuple[str, list[dict]]:
    """All current side panel targets, with parsed tabId."""
    host, targets = _list_targets_resilient()
    out = []
    import re
    for t in targets:
        u = t.get("url", "")
        if not u.startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/") or "sidepanel" not in u.lower():
            continue
        m = re.search(r"[?&]tabId=(\d+)", u)
        out.append({
            "tab_id": int(m.group(1)) if m else None,
            "title": t.get("title", ""),
            "url": u,
            "ws": t.get("webSocketDebuggerUrl", ""),
        })
    return host, out


def cmd_targets() -> int:
    try:
        host, targets = _list_targets_resilient()
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        v = get_version(host, DEBUG_PORT)
        print(f"Chrome: {v.get('Browser')} (proto {v.get('Protocol-Version')}) @ {host}:{DEBUG_PORT}")
    except Exception as e:
        print(f"version probe failed: {e}", file=sys.stderr)
    for t in targets:
        flag = "★" if t.get("url", "").startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/") and "sidepanel" in t.get("url", "").lower() else " "
        print(f"{flag} type={t.get('type'):<14} title={t.get('title', '')[:50]}")
        print(f"     url={t.get('url', '')[:120]}")
        print(f"     ws ={t.get('webSocketDebuggerUrl', '')[:120]}")
    return 0


DISCOVER_JS = r"""
(() => {
  const summarize = (el, max=240) => {
    const attrs = {};
    for (const a of (el.attributes || [])) attrs[a.name] = (a.value || '').slice(0, 120);
    return {
      tag: el.tagName?.toLowerCase?.(),
      id: el.id || undefined,
      classes: (el.className?.toString?.() || '').slice(0, 200),
      attrs,
      text_preview: (el.innerText || el.textContent || '').slice(0, max).trim(),
    };
  };
  const out = { url: location.href, title: document.title, body_html_len: document.body?.innerHTML?.length || 0 };
  out.inputs = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]')).slice(0, 10).map(summarize);
  out.buttons = Array.from(document.querySelectorAll('button')).slice(0, 30).map(b => ({
    text: (b.innerText || '').slice(0, 60),
    ariaLabel: b.getAttribute('aria-label') || '',
    type: b.type, disabled: b.disabled,
    classes: (b.className?.toString?.() || '').slice(0, 120),
  }));
  out.message_like = Array.from(document.querySelectorAll('[role="article"], [data-testid*="message" i], [class*="message" i], [class*="conversation" i]')).slice(0, 12).map(el => summarize(el, 120));
  return out;
})()
"""


def cmd_discover() -> int:
    try:
        host, t = find_sidepanel_target()
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 2
    if not t:
        print("Claude side panel target not found. Make sure:", file=sys.stderr)
        print("  1. Chrome is launched with --remote-debugging-port=9222", file=sys.stderr)
        print("  2. The Claude side panel is OPEN (click the orange Claude icon)", file=sys.stderr)
        print("Run 'cdp_daemon.py targets' to see what Chrome reports.", file=sys.stderr)
        return 2
    log(f"target found via {host}: {t.get('title')} {t.get('url')}")
    # CDP returns ws://127.0.0.1:9222/... — rewrite host to the one that worked.
    ws_url = t["webSocketDebuggerUrl"]
    if host != "127.0.0.1":
        ws_url = ws_url.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
    client = CDPClient(ws_url)
    client.connect()
    try:
        client.send_command("Runtime.enable")
        r = client.send_command("Runtime.evaluate", {
            "expression": DISCOVER_JS,
            "returnByValue": True,
            "awaitPromise": False,
        })
        if "result" in r and r["result"].get("result", {}).get("value") is not None:
            print(json.dumps(r["result"]["result"]["value"], indent=2))
            return 0
        print(json.dumps(r, indent=2))
        return 1
    finally:
        client.close()


PRE_SEND_JS = r"""
(() => {
  // 2026-09-19: Claude for Chrome renamed its DOM. Every selector is now a
  // fallback list, newest first, so the next rename degrades instead of
  // failing with "editor not found". Verified live against
  // https://claude.ai/cic/task/...: editor = [data-testid="chat-input"],
  // send = button[data-testid="chat-input-send"], turns = [data-testid="assistant-message"].
  const __EDITOR = ['[data-testid="chat-input"]', '[data-test-id="message-input"]',
                    'div.ProseMirror[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"]'];
  const __SEND   = ['button[data-testid="chat-input-send"]', 'button[aria-label="Send message"]',
                    'button[aria-label*="Send" i]'];
  const __REPLY  = ['[data-testid="assistant-message"]', '.claude-response', 'div.font-claude-response'];
  const __pick = (list) => { for (const q of list) { const el = document.querySelector(q); if (el) return el; } return null; };
  const __allOf = (list) => { for (const q of list) { const els = document.querySelectorAll(q); if (els.length) return els; } return []; };
  // reason: "signed_out" when the panel shows the Claude "Log in" screen
  // (2026-11-04), "no_conversation" on /cic/new, else "missing".
  const __noEditor = () => {
    const out = [...document.querySelectorAll("button, a")].some(b => /^\s*log in\s*$/i.test(b.textContent || ""))
        || /^\s*log in\s*$/im.test((document.body && document.body.innerText) || "");
    const reason = out ? "signed_out" : /\/cic\/new\b/.test(location.href) ? "no_conversation" : "missing";
    return { ok: false, reason, error: "editor not found at " + location.href + " (" + reason + ")" };
  };
  const editor = __pick(__EDITOR);
  if (!editor) return __noEditor();
  editor.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editor);
  sel.removeAllRanges(); sel.addRange(range);
  if (document.execCommand) document.execCommand("delete", false);
  if (editor.innerText.trim()) editor.innerHTML = "<p></p>";
  editor.focus();
  // The side panel keeps only ONE .claude-response in the DOM (replaced per
  // turn, not appended). So snapshot its current text — we wait for the text
  // to CHANGE post-send, then stabilize.
  const all = __allOf(__REPLY);
  // 2026-09-24: extract exactly as POLL_JS does (response body only, minus
  // "Thought process"). Reading the whole turn here included the screen-reader
  // H2, so baseline (434) never equalled the polled text (401) and the previous
  // reply was captured as the new one the instant the prompt was sent.
  const __lastEl = all.length ? all[all.length - 1] : null;
  const __bodyEl = __lastEl ? (__lastEl.querySelector('.font-claude-response') || __lastEl) : null;
  const baseText = __bodyEl ? (__bodyEl.innerText || "").trim().split("\n")
      .filter(l => l.trim() !== "Thought process").join("\n").trim() : "";
  // Action-bar count before sending: a turn is only finished once ITS bar
  // exists, which shows up as this count going up (see bar_baseline).
  const baseBars = document.querySelectorAll('[data-testid="action-bar-thumbs-up"], [data-testid="action-bar-copy"], button[aria-label="Good response"]').length;
  return { ok: true, baseline_text: baseText, baseline_count: all.length,
           baseline_bars: baseBars,
           editor_empty: editor.innerText.trim().length === 0 };
})()
"""

# Same baseline snapshot as PRE_SEND_JS, minus the clearing. A keys-only
# dispatch (Enter, Escape) must not touch what is already in the composer:
# clearing it first is what made every `browser-bridge keys ... Enter` after a
# successful `type` fail with "editor empty — nothing to send".
PRE_SEND_NOCLEAR_JS = PRE_SEND_JS.replace(
    '  if (document.execCommand) document.execCommand("delete", false);\n', "").replace(
    '  if (editor.innerText.trim()) editor.innerHTML = "<p></p>";\n', "").replace(
    """  editor.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editor);
  sel.removeAllRanges(); sel.addRange(range);
""", "  editor.focus();\n")

CLICK_SEND_JS = r"""
(() => {
  // 2026-09-19: Claude for Chrome renamed its DOM. Every selector is now a
  // fallback list, newest first, so the next rename degrades instead of
  // failing with "editor not found". Verified live against
  // https://claude.ai/cic/task/...: editor = [data-testid="chat-input"],
  // send = button[data-testid="chat-input-send"], turns = [data-testid="assistant-message"].
  const __EDITOR = ['[data-testid="chat-input"]', '[data-test-id="message-input"]',
                    'div.ProseMirror[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"]'];
  const __SEND   = ['button[data-testid="chat-input-send"]', 'button[aria-label="Send message"]',
                    'button[aria-label*="Send" i]'];
  const __REPLY  = ['[data-testid="assistant-message"]', '.claude-response', 'div.font-claude-response'];
  const __pick = (list) => { for (const q of list) { const el = document.querySelector(q); if (el) return el; } return null; };
  const __allOf = (list) => { for (const q of list) { const els = document.querySelectorAll(q); if (els.length) return els; } return []; };
  // reason: "signed_out" when the panel shows the Claude "Log in" screen
  // (2026-11-04), "no_conversation" on /cic/new, else "missing".
  const __noEditor = () => {
    const out = [...document.querySelectorAll("button, a")].some(b => /^\s*log in\s*$/i.test(b.textContent || ""))
        || /^\s*log in\s*$/im.test((document.body && document.body.innerText) || "");
    const reason = out ? "signed_out" : /\/cic\/new\b/.test(location.href) ? "no_conversation" : "missing";
    return { ok: false, reason, error: "editor not found at " + location.href + " (" + reason + ")" };
  };
  const editor = __pick(__EDITOR);
  if (!editor) return __noEditor();
  const expected = __SNIPPET__;
  if (!editor.innerText.includes(expected)) {
    return { ok: false, error: "text not in editor: " + JSON.stringify(editor.innerText.slice(0, 80)) };
  }
  const btn = __pick(__SEND);
  if (!btn) return { ok: false, error: "send button missing (composer has text but no send control appeared)" };
  if (btn.disabled) return { ok: false, error: "send button disabled" };
  btn.click();
  return { ok: true };
})()
"""

# Live task progress (BRIDGE-TASKS-SPEC.md). Read-only: never clicks, expands
# or types. Verified 2026-10-04 against Claude for Chrome: there is no
# collapsed "N steps" button any more. Each tool action renders as a row whose
# label is a [data-cds="ShimmerText"] span (data-shimmering="true" while it
# runs, "false" once done), visible without clicking. The rows after the last
# user message belong to the message in flight. firstChild skips the hidden
# duplicate label the shimmer effect nests inside the span.
PROGRESS_JS = r"""
(() => {
  const users = document.querySelectorAll('[data-testid="user-message"]');
  const lastUser = users[users.length - 1];
  const after = e => !lastUser || (lastUser.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING);
  const rows = [...document.querySelectorAll('[data-cds="ShimmerText"]')].filter(after)
    .map(e => ((e.firstChild && e.firstChild.textContent) || "").trim());
  // Once the turn ends the rows fold into a "Used Claude in Chrome (N actions)"
  // pill and can leave the DOM; its text still carries the count.
  let pill = 0;
  document.querySelectorAll('[data-testid="tool-status-pill"]').forEach(p => {
    const m = after(p) && /\((\d+)\s+actions?\)/.exec(p.textContent || "");
    if (m) pill += parseInt(m[1], 10);
  });
  return { actions: Math.max(rows.length, pill), last_action: rows.length ? rows[rows.length - 1] : "", rows };
})()
"""


POLL_JS = r"""
(() => {
  // 2026-09-19: Claude for Chrome renamed its DOM. Every selector is now a
  // fallback list, newest first, so the next rename degrades instead of
  // failing with "editor not found". Verified live against
  // https://claude.ai/cic/task/...: editor = [data-testid="chat-input"],
  // send = button[data-testid="chat-input-send"], turns = [data-testid="assistant-message"].
  const __EDITOR = ['[data-testid="chat-input"]', '[data-test-id="message-input"]',
                    'div.ProseMirror[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"]'];
  const __SEND   = ['button[data-testid="chat-input-send"]', 'button[aria-label="Send message"]',
                    'button[aria-label*="Send" i]'];
  const __REPLY  = ['[data-testid="assistant-message"]', '.claude-response', 'div.font-claude-response'];
  const __pick = (list) => { for (const q of list) { const el = document.querySelector(q); if (el) return el; } return null; };
  const __allOf = (list) => { for (const q of list) { const els = document.querySelectorAll(q); if (els.length) return els; } return []; };
  // reason: "signed_out" when the panel shows the Claude "Log in" screen
  // (2026-11-04), "no_conversation" on /cic/new, else "missing".
  const __noEditor = () => {
    const out = [...document.querySelectorAll("button, a")].some(b => /^\s*log in\s*$/i.test(b.textContent || ""))
        || /^\s*log in\s*$/im.test((document.body && document.body.innerText) || "");
    const reason = out ? "signed_out" : /\/cic\/new\b/.test(location.href) ? "no_conversation" : "missing";
    return { ok: false, reason, error: "editor not found at " + location.href + " (" + reason + ")" };
  };
  const all = __allOf(__REPLY);
  if (all.length === 0) return { count: 0, len: 0, text: "", finalized: false };
  const last = all[all.length - 1];
  // 2026-09-19: read only the response body. The turn element also holds a
  // screen-reader H2 ("Claude responded: ...") that duplicates the text, and
  // while the model is still working it shows collapsed "Thought process"
  // blocks. Both leaked into captured replies ("Thought process / READY").
  const body = last.querySelector('.font-claude-response') || last;
  let txt = (body.innerText || "").trim();
  txt = txt.split("\n").filter(l => l.trim() !== "Thought process").join("\n").trim();
  // Finalized means THIS turn's action bar exists. The old check took any
  // "Good response" button in the document, so an earlier turn's bar marked a
  // reply final before it had streamed, and a 3-second lull mid-stream got
  // captured as the answer.
  // 2026-09-19b: finalization must never be able to stick false, or the watch
  // burns its whole timeout on a reply that is already on screen (observed:
  // 1800s timeout with last_len=3931). Two conditions from the first attempt
  // did exactly that and are gone:
  //   - a response-status-announcer regex on /respond/, which stays true
  //     because the finished state reads "Claude responded: ...";
  //   - an action-bar lookup scoped to [data-testid="transcript-row"], which
  //     finds nothing when the bar renders outside that container.
  // Prefer the row-scoped bar for precision, fall back to document-wide.
  // Staleness is already handled by the caller: it requires the text to
  // differ from baseline AND hold steady for STABLE_THRESHOLD_S.
  const row = last.closest('[data-testid="transcript-row"]') || last.parentElement;
  const BAR = '[data-testid="action-bar-thumbs-up"], [data-testid="action-bar-copy"], button[aria-label="Good response"]';
  const rowBar = row ? !!row.querySelector(BAR) : false;
  const anyBar = !!document.querySelector(BAR);
  const bar = rowBar || anyBar;
  const stopBtn = !!document.querySelector('button[aria-label*="Stop" i], button[data-testid="chat-input-stop"]');
  const thinkingOnly = txt.length === 0 && /Thought process/.test(last.innerText || "");
  // 2026-09-24: "finished" means THIS turn has its own action bar. Climb from
  // the last reply while the container still holds only that one reply; a bar
  // found on the way belongs to it. The document-wide fallback found the
  // previous turn's bar, so an agent mid-task ("Opening tab") read as done and
  // the watch closed before any page was opened. Verified live: every finished
  // turn has its bar one level up; an in-progress turn has none.
  let ownBar = false;
  { // Only the selector that matched: the list also holds the nested body
    // (.font-claude-response), which made every parent look multi-reply.
    const M = __REPLY.find(q => document.querySelectorAll(q).length) || __REPLY[0];
    let el = last;
    while (el && el !== document.body) {
      if (el.querySelector(BAR)) { ownBar = true; break; }
      const par = el.parentElement;
      if (!par || par.querySelectorAll(M).length > 1) break;
      el = par;
    } }
  const finalized = ownBar && !stopBtn && !thinkingOnly;
  const barCount = document.querySelectorAll(BAR).length;
  return { count: all.length, len: txt.length, text: txt.slice(0, 16000),
           finalized, good: bar, rowBar, stop: stopBtn, thinkingOnly, barCount };
})()
"""


def _eval_value(client: CDPClient, expression: str, await_promise: bool = False, timeout: float = 30):
    r = client.send_command("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    }, timeout=timeout)
    if "exceptionDetails" in (r.get("result") or {}):
        raise RuntimeError(f"page script exception: {r['result']['exceptionDetails']}")
    if "result" not in r:
        raise RuntimeError(f"unexpected CDP response: {json.dumps(r)[:400]}")
    return r["result"].get("result", {}).get("value")


def resolve_agent_port(agent_name: str) -> int | None:
    """Look up agent name → CDP port from the bridge's /agents endpoint."""
    try:
        status, payload = bridge("GET", "/agents")
    except Exception as e:
        log(f"agent lookup failed: {e}")
        return None
    if status != 200 or not isinstance(payload, dict):
        return None
    info = (payload.get("agents") or {}).get(agent_name)
    if not info:
        return None
    return info.get("port")


def _list_targets_at(host: str, port: int) -> list[dict]:
    return list_targets(host, port)


def find_sidepanel_target_at(host: str, port: int, tab_id: int | None = None) -> dict | None:
    """Find a Claude side panel in the Chrome instance at host:port.

    If tab_id is given, prefer the panel whose URL contains ?tabId=<tab_id>.
    Falls back to the first side panel found if no tab_id (or no match)."""
    try:
        targets = _list_targets_at(host, port)
    except Exception:
        return None
    panels = [t for t in targets
              if t.get("url", "").startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/")
              and "sidepanel" in t.get("url", "").lower()]
    if tab_id is not None:
        for t in panels:
            if f"tabId={tab_id}" in t.get("url", ""):
                return t
        return None  # explicit tab_id: fail loudly if not found
    return panels[0] if panels else None


def chat_frame_target(host: str, port: int, panel: dict) -> dict:
    """Since the September 2026 Claude for Chrome update the chat UI lives in a
    cross-origin iframe (https://claude.ai/cic/...). That iframe is its own CDP
    target, listed by /json with parentId == the panel page's id, and it is the
    only place the composer, the send button and the assistant turns exist. The
    panel page itself has no editor at all, which is why every dispatch failed
    with "editor not found" on 2026-09-19. Return the frame when present, else
    the panel page (older builds rendered the chat in-page)."""
    try:
        targets = _list_targets_at(host, port)
    except Exception:
        return panel
    pid = panel.get("id")
    frames = [t for t in targets
              if (t.get("type") == "iframe" and t.get("parentId") == pid
                  and (t.get("url") or "").startswith("https://claude.ai/"))]
    if not frames:
        return panel
    if len(frames) == 1:
        return frames[0]
    # More than one claude.ai frame under the same panel: the panel navigated
    # to a new conversation and the previous frame is still listed. Typing into
    # the old one succeeds at the CDP level and never appears on screen, which
    # is the silent "dispatch ok, composer empty" failure. Ask each frame
    # whether it still has a composer and is the visible document, newest last.
    log(f"chat_frame_target: {len(frames)} claude.ai frames under panel {pid}, probing for the live one")
    PROBE = ('(() => ({ editor: !!(document.querySelector(\'[data-testid="chat-input"]\')'
             ' || document.querySelector(\'div.ProseMirror[contenteditable="true"]\')),'
             ' visible: document.visibilityState === "visible" }))()')
    for t in reversed(frames):
        ws = t.get("webSocketDebuggerUrl") or ""
        if host != "127.0.0.1":
            ws = ws.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
        probe_client = None
        try:
            probe_client = CDPClient(ws)
            probe_client.connect()
            probe_client.send_command("Runtime.enable")
            r = _eval_value(probe_client, PROBE, timeout=5)
            if isinstance(r, dict) and r.get("editor") and r.get("visible"):
                return t
        except Exception:
            pass
        finally:
            if probe_client is not None:
                try: probe_client.close()
                except Exception: pass
    return frames[-1]


SHORTS_FILE = Path.home() / ".config" / "claude-web-bridge" / "panel-shorts.json"


def _panel_alias(tab_id) -> str:
    """The /brN alias the server gave this tab (panel-shorts.json), else tab:<id>."""
    try:
        n = json.loads(SHORTS_FILE.read_text())["map"][str(tab_id)]
        return f"/br{n}"
    except Exception:
        return f"tab:{tab_id}"


def _no_editor_text(res: dict, tab_id) -> str:
    """Plain-text error for a panel with no editor (2026-11-04: the raw dict
    pointed at a nonexistent `browser-bridge discover` even when the panel was
    simply signed out)."""
    panel = _panel_alias(tab_id)
    reason = res.get("reason")
    if reason == "signed_out":
        return (f"panel {panel} is signed out of Claude.\n"
                f'Click "Log in" in the side panel of the bridge Chrome window, '
                f"then send the message again.")
    if reason == "no_conversation":
        return f"panel {panel} has no conversation yet: send one message in it first."
    return f"panel {panel} has no text box to type into.\nRun: browser-bridge doctor"


def dispatch_to_sidepanel(item: dict, timing: dict | None = None) -> tuple[bool, str]:
    """Returns (ok, text_or_err). If `timing` dict is provided, fills it with
    keys: t_dispatch_start, t_typed, t_clicked, t_stream_done, t_capture,
    t_reply_ready (all unix-time floats)."""
    if timing is None:
        timing = {}
    timing["t_dispatch_start"] = time.time()
    # Each agent has its own Chrome profile + debug port. Resolve the agent's
    # port and connect there — the side panel is whatever Claude side panel is
    # open in that Chrome instance.
    agent = item.get("to_agent")
    if not agent:
        return False, "no agent name on item; sidepanel mode requires --to <agent>"
    port = resolve_agent_port(agent)
    if port is None:
        return False, (f"agent '{agent}' not registered. Run: "
                       f"claude-web agents create {agent}")
    requested_tab_id = item.get("tab_id")
    # 2026-09-19: this path used DEBUG_HOST alone, which in WSL was 127.0.0.1,
    # so every dispatch reported "no Claude side panel" while the panel sat on
    # the Windows host one entry later in the list. Walk the candidates and
    # take the first host that actually shows the panel.
    host = DEBUG_HOST
    t = None
    last_exc: Exception | None = None
    for cand in _try_hosts():
        try:
            tt = find_sidepanel_target_at(cand, port, tab_id=requested_tab_id)
        except Exception as e:
            last_exc = e
            continue
        host = cand
        if tt:
            t = tt
            break
    if t is None and last_exc is not None and host == DEBUG_HOST:
        return False, str(last_exc)
    if not t:
        if requested_tab_id is not None:
            return False, (f"no Claude side panel with tabId={requested_tab_id} in agent '{agent}' "
                           f"Chrome instance ({host}:{port}).")
        return False, (f"no Claude side panel found in agent '{agent}' Chrome instance "
                       f"({host}:{port}). Make sure Chrome for this agent is launched and "
                       f"the side panel is open.")
    frame = chat_frame_target(host, port, t)
    if frame is not t:
        log(f"dispatch: using chat iframe {frame.get('url','')[:60]} inside panel tab {requested_tab_id}")
    ws_url = frame["webSocketDebuggerUrl"]
    if host != "127.0.0.1":
        ws_url = ws_url.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
    client = CDPClient(ws_url)
    try:
        client.connect()
        client.send_command("Runtime.enable")

        # Step 0: wait for the panel to be idle before touching it.
        # The Send button does not exist while a turn is running — the panel
        # swaps it for Stop — so typing into a busy panel ends in
        # "send button missing" and the dispatch is lost. Worse, the text sits
        # in the composer where the next thing to type there (a panel greeting,
        # say) silently overwrites it. Both were observed back to back.
        BUSY_WAIT_S = float(os.environ.get("CLAUDE_BRIDGE_BUSY_WAIT_S", "900"))
        waited = 0.0
        while waited < BUSY_WAIT_S:
            state = _eval_value(client, POLL_JS, timeout=8)
            if not (isinstance(state, dict) and state.get("stop")):
                break
            if waited == 0:
                log("panel is mid-turn, waiting for it to finish before sending")
            time.sleep(3.0); waited += 3.0
        else:
            return False, (f"panel still mid-turn after {BUSY_WAIT_S:.0f}s; "
                           f"not typing into a busy composer")
        if waited:
            log(f"panel went idle after {waited:.0f}s, proceeding")

        # Step 1: capture baseline copy-message count, and clear the editor
        # UNLESS this dispatch only sends keys. A keys-only Enter submits
        # whatever a previous `type` left in the composer, so clearing first
        # destroys the very text it was asked to send.
        auto_submit = item.get("auto_submit", True)
        keys_only = bool(item.get("keys_only", False))
        keys_to_send = item.get("keys") or []
        pre = _eval_value(client, PRE_SEND_NOCLEAR_JS if keys_only else PRE_SEND_JS,
                          timeout=10)
        # A panel opened a second ago has not drawn its composer yet: the
        # first message to a fresh /br panel failed with "editor not found"
        # (2026-10-04). Give a missing editor up to 15 s before failing.
        waited_editor = 0.0
        while (isinstance(pre, dict) and not pre.get("ok")
               and "editor not found" in str(pre.get("error", "")) and waited_editor < 15):
            time.sleep(1.0); waited_editor += 1.0
            pre = _eval_value(client, PRE_SEND_NOCLEAR_JS if keys_only else PRE_SEND_JS,
                              timeout=10)
        if waited_editor:
            log(f"waited {waited_editor:.0f}s for the panel's editor: "
                f"{'ok' if isinstance(pre, dict) and pre.get('ok') else 'still missing'}")
        if not isinstance(pre, dict) or not pre.get("ok"):
            if isinstance(pre, dict) and pre.get("reason"):
                return False, _no_editor_text(pre, item.get("tab_id"))
            return False, f"pre-send setup failed: {pre}"
        baseline_text = pre.get("baseline_text", "")
        bar_baseline = pre.get("baseline_bars")
        # Kept for late capture: it needs to tell a genuinely new reply from
        # the text that was already on screen before this dispatch.
        _last_baseline[item.get("id", "")] = baseline_text
        log(f"pre-send: baseline reply len = {len(baseline_text)}, count = {pre.get('baseline_count')}, editor empty = {pre.get('editor_empty')}")

        # Step 2: type the text via real keyboard input.
        if keys_only:
            # SMUX-style atomic submit: when key=Enter, click the Send
            # button via JS instead of dispatching raw Input.dispatchKeyEvent
            # (claude.ai's send is a click handler, not a keypress handler —
            # documented 2026-05-11 in tmux-browser-bridge/SKILL.md).
            # Other keys (Escape, Tab) still go through Input.dispatchKeyEvent.
            _KEY_MAP = {
                "Enter":  {"windowsVirtualKeyCode": 13, "key": "Enter",  "code": "Enter"},
                "Escape": {"windowsVirtualKeyCode": 27, "key": "Escape", "code": "Escape"},
                "Tab":    {"windowsVirtualKeyCode":  9, "key": "Tab",    "code": "Tab"},
            }
            CLICK_SEND_ANY_TEXT_JS = r"""
            (() => {
              // 2026-09-19: Claude for Chrome renamed its DOM. Every selector is now a
              // fallback list, newest first, so the next rename degrades instead of
              // failing with "editor not found". Verified live against
              // https://claude.ai/cic/task/...: editor = [data-testid="chat-input"],
              // send = button[data-testid="chat-input-send"], turns = [data-testid="assistant-message"].
              const __EDITOR = ['[data-testid="chat-input"]', '[data-test-id="message-input"]',
                                'div.ProseMirror[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"]'];
              const __SEND   = ['button[data-testid="chat-input-send"]', 'button[aria-label="Send message"]',
                                'button[aria-label*="Send" i]'];
              const __REPLY  = ['[data-testid="assistant-message"]', '.claude-response', 'div.font-claude-response'];
              const __pick = (list) => { for (const q of list) { const el = document.querySelector(q); if (el) return el; } return null; };
              const __allOf = (list) => { for (const q of list) { const els = document.querySelectorAll(q); if (els.length) return els; } return []; };
              // reason: "signed_out" when the panel shows the Claude "Log in" screen
              // (2026-11-04), "no_conversation" on /cic/new, else "missing".
              const __noEditor = () => {
                const out = [...document.querySelectorAll("button, a")].some(b => /^\s*log in\s*$/i.test(b.textContent || ""))
                    || /^\s*log in\s*$/im.test((document.body && document.body.innerText) || "");
                const reason = out ? "signed_out" : /\/cic\/new\b/.test(location.href) ? "no_conversation" : "missing";
                return { ok: false, reason, error: "editor not found at " + location.href + " (" + reason + ")" };
              };
              const editor = __pick(__EDITOR);
              if (!editor) return __noEditor();
              if (!editor.innerText.trim()) return { ok: false, error: "editor empty — nothing to send" };
              const btn = __pick(__SEND);
              if (!btn) return { ok: false, error: "send button missing" };
              if (btn.disabled) return { ok: false, error: "send button disabled" };
              btn.click();
              return { ok: true };
            })()
            """
            for k in keys_to_send:
                if k == "Enter":
                    try:
                        click_res = _eval_value(client, CLICK_SEND_ANY_TEXT_JS, timeout=10)
                    except Exception as e:
                        return False, f"keys-only Enter (click-send) failed: {e}"
                    if not isinstance(click_res, dict) or not click_res.get("ok"):
                        if isinstance(click_res, dict) and click_res.get("reason"):
                            return False, _no_editor_text(click_res, item.get("tab_id"))
                        return False, f"keys-only Enter (click-send) refused: {click_res}"
                    continue
                base = _KEY_MAP.get(k, {"key": k, "code": k})
                down = {"type": "keyDown", **base}
                up = {"type": "keyUp", **base}
                try:
                    client.send_command("Input.dispatchKeyEvent", down, timeout=5)
                    client.send_command("Input.dispatchKeyEvent", up, timeout=5)
                except Exception as e:
                    return False, f"keys-only dispatch failed on key {k!r}: {e}"
            timing["t_typed"] = time.time()
            timing["t_clicked"] = time.time()
            try: client.close()
            except Exception: pass
            return True, "[keys-only dispatch ok]"

        client.send_command("Input.insertText", {"text": item["text"]}, timeout=10)
        time.sleep(0.3)
        timing["t_typed"] = time.time()

        # Read the composer back. Input.insertText returns success whenever the
        # CDP call itself was accepted, including when it went into a document
        # nobody is looking at (a stale chat frame, a panel mid-reload). The
        # dispatch then reported ok while the visible composer stayed empty,
        # and the follow-up Enter was refused with "editor empty". A readback
        # is the only thing that distinguishes the two.
        READBACK_JS = r"""
        (() => {
          const __EDITOR = ['[data-testid="chat-input"]', '[data-test-id="message-input"]',
                            'div.ProseMirror[contenteditable="true"]', 'div[contenteditable="true"][role="textbox"]'];
          let editor = null;
          for (const q of __EDITOR) { const el = document.querySelector(q); if (el) { editor = el; break; } }
          if (!editor) return { ok: false, error: "editor vanished after typing at " + location.href };
          const t = editor.innerText || "";
          return { ok: true, len: t.length, head: t.slice(0, 40), url: location.href };
        })()
        """
        back = _eval_value(client, READBACK_JS, timeout=10)
        if not isinstance(back, dict) or not back.get("ok"):
            return False, f"typed text could not be verified: {back}"
        # Compare with whitespace removed: the composer is a rich-text editor
        # and re-flows newlines and runs of spaces, so only the characters are
        # a fair check.
        _squash = lambda x: re.sub(r"\s+", "", x or "")
        want_head = _squash(item["text"])[:20]
        got_head = _squash(back.get("head"))[:20]
        if not got_head or (want_head and not want_head.startswith(got_head[:12])):
            return False, ("typed text did not reach the composer (CDP accepted it but the "
                           f"editor holds {back.get('len')} chars starting {back.get('head')!r}; "
                           f"frame {back.get('url','')[:80]}). The panel is probably showing a "
                           "different conversation or a login screen than the frame the bridge "
                           "is driving: reopen the side panel and retry.")
        log(f"typed {len(item['text'])} chars, composer holds {back.get('len')} chars")

        if not auto_submit:
            # Text typed but not submitted; skip click and read-loop.
            try: client.close()
            except Exception: pass
            return True, f"[type-only dispatch ok: {back.get('len')} chars in composer]"

        # Step 3: click Send (synchronous JS — no long-running awaitPromise).
        snippet = item["text"][:8] if len(item["text"]) >= 8 else item["text"]
        click_expr = CLICK_SEND_JS.replace("__SNIPPET__", json.dumps(snippet))
        click_res = _eval_value(client, click_expr, timeout=10)
        if not isinstance(click_res, dict) or not click_res.get("ok"):
            if isinstance(click_res, dict) and click_res.get("reason"):
                return False, _no_editor_text(click_res, item.get("tab_id"))
            return False, f"click send failed: {click_res}"
        timing["t_clicked"] = time.time()

        # Step 4: poll-from-Python for the assistant reply. Each CDP call is a
        # quick synchronous evaluate, so the WebSocket can't get stuck on a
        # long-running awaitPromise. Stability detected when text length stays
        # unchanged for SETTLE_S, and the text differs from baseline.
        # 2026-06-07: both knobs env-configurable + per-dispatch overridable.
        # Deep-research dispatches routinely exceed the old fixed 600s (the
        # agent works for many minutes before any reply text appears), which
        # made the daemon report "failures" for tasks that were still running.
        TIMEOUT_S = int(item.get("timeout_s") or os.environ.get("CLAUDE_BRIDGE_WATCH_TIMEOUT_S", "1800"))
        SETTLE_S = 3.0
        POLL_INT = 0.4
        # Raised 2026-05-10 from 12s to 60s. The previous value tripped on any
        # dispatch whose first step was a navigation, a Google search, or a
        # multi-second page load — common cases. 60s is long enough that a
        # truly wedged input (rate-limit / stale chat) still surfaces, but a
        # normal slow-start reply doesn't get falsely reported as failed.
        # 2026-06-07: the stuck detector is now ACTIVITY-AWARE — before erroring
        # it asks the server whether the extension reports the tab as working/
        # streaming; if yes, the clock resets (agent is alive, just not writing
        # reply text yet). So this value only fires on truly dead prompts.
        STUCK_AT_BASELINE_S = float(os.environ.get("CLAUDE_BRIDGE_STUCK_S", "240"))
        deadline = time.monotonic() + TIMEOUT_S
        last_len = -1
        stable_since = time.monotonic()
        last_log = 0.0
        captured: dict | None = None
        # Wedge-detection state: when did we last see `differs=True`? If never,
        # the prompt didn't actually go in or chat is silently rejecting.
        last_differed_at: float | None = None
        activity_resets = 0  # 2026-06-07b: cap resets — a stale activity feed
        # must not disable the stuck detector forever (18-min hang observed)
        # 2026-06-07 chat-reset detector: if the panel text VANISHES (len 0
        # while baseline was non-empty), the chat was closed/reset mid-watch.
        # Without this, differs=True (empty != baseline) disables the stuck
        # detector and the watch hangs until TIMEOUT_S holding the tab lock.
        zero_since: float | None = None
        CHAT_RESET_S = 45.0
        # 2026-08-30: the reset detector used to fire on any 45s of empty reply
        # text. But a panel that COMPACTS its conversation empties that area
        # too, and so does a long tool-use run before the first token lands —
        # both while the agent is very much alive. Failing there is what loses
        # a finished report (patch note 6). So it now consults the same
        # activity feed the stuck detector uses, and on a live tab re-baselines
        # instead of failing: after a compaction the old baseline no longer
        # exists, so "" is the correct new baseline and the next reply
        # registers as a change rather than as more emptiness.
        chat_reset_resets = 0
        # Seeded from the pre-send snapshot, not 0: this counts assistant turns
        # that appeared AFTER this dispatch, and starting at 0 made the very
        # first poll look like growth.
        baseline_turns = int(pre.get("baseline_count") or 0)
        last_turn_count = baseline_turns
        turn_count_grew = False
        first_finalized_after_send_at: float | None = None  # for t_stream_done metric

        def tab_reports_activity() -> bool:
            """True while the extension still sees this tab working/streaming."""
            try:
                _st, act = bridge("GET", "/panels/activity")
                if not isinstance(act, dict):
                    return False
                tid = item.get("tab_id")
                busy = set(act.get("working_tab_ids", []) + act.get("streaming_tab_ids", []))
                last = (act.get("last_active_tab") or {}).get(str(tid), 0)
                return (tid in busy) or (act.get("now", 0) - last < 60)
            except Exception as e:
                log(f"activity check failed (treating as inactive): {e}")
                return False
        # Long-lived connection turned out to be unreliable — Chrome's execution
        # context invalidates when the agent action navigates tabs. Open a fresh
        # connection per poll instead. Diagnostic timing logged per iteration.
        try: client.close()
        except Exception: pass
        client = None  # type: ignore
        POLL_INT = 0.3  # tighter cadence; wedge detector now catches stuck loops
        last_progress_at = 0.0
        last_progress = None
        STABLE_THRESHOLD_S = 0.25  # was 1.0; finalize signal is reliable enough that this only catches trailing tokens
        iter_n = 0

        agent_port = port  # captured for closure-free use below

        while time.monotonic() < deadline:
            iter_n += 1
            t_iter = time.monotonic()
            state = None
            try:
                pt = find_sidepanel_target_at(host, agent_port, tab_id=requested_tab_id)
                t_target = time.monotonic()
                if not pt:
                    log(f"iter#{iter_n}: side panel target gone, retrying"); time.sleep(1.0); continue
                pws = chat_frame_target(host, agent_port, pt)["webSocketDebuggerUrl"]
                if host != "127.0.0.1":
                    pws = pws.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
                pc = CDPClient(pws)
                pc.connect()
                t_connect = time.monotonic()
                try:
                    pc.send_command("Runtime.enable", timeout=5)
                    t_runen = time.monotonic()
                    state = _eval_value(pc, POLL_JS, timeout=8)
                    t_eval = time.monotonic()
                    # Task progress every ~3 s, and on every poll once the
                    # reply is final so the last count is the true total.
                    if (item.get("task_id")
                            and ((t_eval - last_progress_at) >= 3.0
                                 or (isinstance(state, dict) and state.get("finalized")))):
                        last_progress_at = t_eval
                        try:
                            prog = _eval_value(pc, PROGRESS_JS, timeout=5)
                            if isinstance(prog, dict):
                                out = {"actions": prog.get("actions", 0),
                                       "last_action": prog.get("last_action") or ""}
                                # Never report fewer actions than already seen,
                                # and keep the last action once the rows fold.
                                if last_progress:
                                    out["actions"] = max(out["actions"], last_progress["actions"])
                                    out["last_action"] = out["last_action"] or last_progress["last_action"]
                                if item.get("task_id") and out != last_progress:
                                    bridge("POST", "/dispatch/progress", {"id": item["id"], **out})
                                last_progress = out
                        except Exception as e:
                            log(f"  task progress read failed: {e}")
                finally:
                    pc.close()
                t_close = time.monotonic()
                if iter_n <= 3 or iter_n % 5 == 0:
                    log(f"iter#{iter_n} timings: target={t_target-t_iter:.2f}s connect={t_connect-t_target:.2f}s "
                        f"runen={t_runen-t_connect:.2f}s eval={t_eval-t_runen:.2f}s close={t_close-t_eval:.2f}s")
            except Exception as e:
                log(f"iter#{iter_n} error: {type(e).__name__}: {e}")
                time.sleep(POLL_INT); continue
            if not isinstance(state, dict):
                log(f"iter#{iter_n}: unexpected state {state!r}")
                time.sleep(POLL_INT); continue
            txt = state.get("text", "")
            ln = state.get("len", 0)
            finalized = bool(state.get("finalized"))

            # New turns appearing is a second liveness signal independent of
            # both the reply text and the extension feed. A compaction can drop
            # the count, so only growth counts.
            turn_now = int(state.get("count") or 0)
            turn_count_grew = turn_now > last_turn_count
            last_turn_count = max(last_turn_count, turn_now)
            # A reply whose text equals the previous one is still a reply. Two
            # panels answering an intro with "ok" produced identical text, so a
            # text-only comparison saw no change and the dispatch sat for 240s
            # before reporting "prompt did not reach the model" — while the
            # answer was on screen. An assistant turn that did not exist before
            # the dispatch is the answer, whatever it says.
            new_turn = turn_now > baseline_turns
            differs = txt != baseline_text or new_turn
            # Capture when: text differs from baseline AND non-empty AND the
            # "Good response" button is visible AND no Stop button (i.e., the
            # turn is finalized). Add a small 1s cushion for trailing tokens.
            # Wedge detector: if reply text never changes from baseline,
            # the prompt almost certainly didn't reach the model.
            # Chat-reset fast-fail: text vanished while baseline was non-empty.
            if ln == 0 and len(baseline_text) > 0:
                if zero_since is None:
                    zero_since = time.monotonic()
                elif time.monotonic() - zero_since > CHAT_RESET_S:
                    # A live agent mid-tool-use looks identical to a wiped chat
                    # through `len` alone. The Stop button is the difference:
                    # the panel renders it only while a turn is running, and it
                    # is read from the panel's own DOM, so it stays truthful
                    # even when the extension's activity feed has gone stale
                    # (observed: both busy lists empty during an active run).
                    alive = bool(state.get("stop")) or turn_count_grew or tab_reports_activity()
                    if alive and chat_reset_resets < 5:
                        chat_reset_resets += 1
                        log(f"reply area empty {CHAT_RESET_S:.0f}s BUT panel is alive "
                            f"(stop={state.get('stop')}, count={state.get('count')}) — "
                            f"treating as compaction, re-baselining to empty "
                            f"({chat_reset_resets}/5)")
                        baseline_text = ""
                        zero_since = None
                        stable_since = time.monotonic()
                        last_differed_at = None
                    else:
                        return False, (f"panel chat appears RESET/CLOSED — reply area "
                                       f"empty for {CHAT_RESET_S:.0f}s (baseline was "
                                       f"{len(baseline_text)} chars), no Stop button and no "
                                       f"reported activity. Re-dispatch to the fresh chat.")
            else:
                zero_since = None

            if differs:
                last_differed_at = time.monotonic()
                if "t_stream_started" not in timing:
                    timing["t_stream_started"] = time.time()
                    # Notify the server so the popup can flip the dot from
                    # blue (working) to pink (streaming) right away.
                    try:
                        bridge("POST", "/dispatch/streaming", {
                            "id": item["id"],
                            "tab_id": item.get("tab_id"),
                        })
                    except Exception as e:
                        log(f"streaming notify failed: {e}")
                if finalized and first_finalized_after_send_at is None:
                    first_finalized_after_send_at = time.time()
                    timing["t_stream_done"] = first_finalized_after_send_at
            elif last_differed_at is None:
                stuck_for = time.monotonic() - stable_since
                if stuck_for > STUCK_AT_BASELINE_S:
                    # Activity-aware check (2026-06-07): the agent may be deep in
                    # tool-use (browsing, screenshots) without having written any
                    # reply text yet. Ask the server whether the extension sees
                    # this tab working/streaming before declaring the prompt dead.
                    tab_active = False
                    try:
                        _st, act = bridge("GET", "/panels/activity")
                        if isinstance(act, dict):
                            tid = item.get("tab_id")
                            now_ts = act.get("now", 0)
                            busy = set(act.get("working_tab_ids", []) + act.get("streaming_tab_ids", []))
                            last = (act.get("last_active_tab") or {}).get(str(tid), 0)
                            tab_active = (tid in busy) or (now_ts - last < 60)
                    except Exception as e:
                        log(f"activity check failed (treating as inactive): {e}")
                    # 2026-08-30: the extension's activity feed has been seen
                    # empty during a genuinely active run, so it cannot be the
                    # only witness here either. The Stop button comes from the
                    # panel's own DOM and is present exactly while a turn runs,
                    # which is the same reasoning the chat-reset detector uses.
                    # A running turn also gets unlimited resets: the cap exists
                    # to stop a stale feed hanging the watch forever, and this
                    # signal cannot go stale.
                    if state.get("stop"):
                        stable_since = time.monotonic()
                        log(f"stuck-at-baseline {stuck_for:.0f}s BUT Stop button present — "
                            f"turn is still running, clock reset")
                    elif tab_active and activity_resets < 3:
                        activity_resets += 1
                        log(f"stuck-at-baseline {stuck_for:.0f}s BUT tab reports recent activity — clock reset ({activity_resets}/3)")
                        stable_since = time.monotonic()
                    else:
                        return False, (f"reply text never changed from baseline ({ln} chars) "
                                       f"after {stuck_for:.1f}s — prompt did not reach the model "
                                       f"(stale chat? rate-limit? input not accepted?)")

            if ln > 0 and differs and finalized:
                if ln == last_len:
                    if (time.monotonic() - stable_since) >= STABLE_THRESHOLD_S:
                        captured = {"text": txt}
                        timing["t_capture"] = time.time()
                        log(f"iter#{iter_n}: CAPTURED len={ln} (finalized + stable {time.monotonic() - stable_since:.2f}s)")
                        break
                else:
                    last_len = ln
                    stable_since = time.monotonic()
            elif ln > 0 and differs and ln == last_len:
                # Length stable but not yet finalized; reset the stable clock so
                # we don't capture early during a streaming pause.
                pass
            else:
                # Only reset stability clock when length actually changed.
                # If `differs` is False (still showing baseline), keep clock so
                # the wedge detector above can fire.
                if ln != last_len:
                    last_len = ln
                    stable_since = time.monotonic()
            log(f"iter#{iter_n}: len={ln} differs={differs} final={finalized} stable_for={time.monotonic() - stable_since:.1f}s")

            time.sleep(POLL_INT)
        if not captured:
            return False, f"timeout after {TIMEOUT_S}s (last_len={last_len}, baseline_len={len(baseline_text)})"
        text_out = captured["text"]
        log(f"posting reply ({len(text_out)} chars) to bridge")
        return True, text_out
    except Exception as e:
        return False, f"CDP send failed: {e}"
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass


# ── Per-tab locks: two dispatches to the SAME panel must not interleave
#    (they'd type into one chat and clobber each other). Different tabs run
#    fully in parallel.
_tab_locks_guard = threading.Lock()
_tab_locks: dict = {}

# 2026-08-30 late capture. A watch that aborts orphans its reply permanently:
# the agent keeps working, finishes, writes its report into the panel, and
# nobody is listening any more. The caller gets "[bridge error] ..." and the
# actual work is only recoverable by hand-scraping the DOM. So a failed watch
# now leaves a reader behind, which keeps polling the panel and posts the reply
# if one lands. Two things keep it honest: it never takes the tab lock, so it
# cannot delay a new dispatch, and it aborts the moment a newer dispatch claims
# the same tab, so it can never attribute a fresh reply to the stale request.
_tab_generation: dict = {}
_last_baseline: dict = {}
LATE_CAPTURE_S = float(os.environ.get("CLAUDE_BRIDGE_LATE_CAPTURE_S", "1200"))


def _tab_lock(tab_id) -> threading.Lock:
    key = tab_id if tab_id is not None else "__none__"
    with _tab_locks_guard:
        lk = _tab_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _tab_locks[key] = lk
        return lk


def _post_reply(item: dict, text: str, timing: dict | None = None) -> None:
    """POST /reply. Safe to call twice for one dispatch:
    the server falls back to the body's from_pane once the id has been
    consumed, so a late reply still reaches the caller and still refreshes
    /panels/<tab>/last-reply (errors deliberately do not refresh it)."""
    try:
        body = {
            "id": item["id"],
            "from_pane": item.get("from_pane"),
            "from_agent": item.get("to_agent"),
            "text": text,
        }
        if timing:
            body["daemon_timing"] = timing
        status, _ = bridge("POST", "/reply", body)
        log(f"/reply HTTP {status} id={item.get('id')}")
    except Exception as e:
        log(f"reply post failed id={item.get('id')}: {e}")


def _late_capture(item: dict, generation: int, deadline_s: float = LATE_CAPTURE_S) -> None:
    """Keep reading a panel after its watch gave up, and deliver a reply if the
    agent produces one. Runs unlocked and yields to any newer dispatch."""
    tab_id = item.get("tab_id")
    did = item.get("id", "")
    baseline = _last_baseline.get(did, "")
    # Agents can sit on different Chrome instances, so resolve the same port
    # the dispatch used rather than assuming the daemon default.
    port = resolve_agent_port(item.get("to_agent") or "") or DEBUG_PORT
    end = time.monotonic() + deadline_s
    log(f"late-capture armed id={did} tab={tab_id} for {deadline_s:.0f}s")
    seen_stable_at: float | None = None
    last_len = -1
    while time.monotonic() < end:
        time.sleep(5.0)
        if _tab_generation.get(tab_id) != generation:
            log(f"late-capture id={did}: newer dispatch claimed tab {tab_id}, standing down")
            _last_baseline.pop(did, None)
            return
        try:
            pt = find_sidepanel_target_at(DEBUG_HOST, port, tab_id=tab_id)
            if not pt:
                continue
            pws = chat_frame_target(DEBUG_HOST, port, pt)["webSocketDebuggerUrl"]
            if DEBUG_HOST != "127.0.0.1":
                pws = pws.replace("ws://127.0.0.1:", f"ws://{DEBUG_HOST}:", 1)
            pc = CDPClient(pws); pc.connect()
            try:
                pc.send_command("Runtime.enable", timeout=5)
                state = _eval_value(pc, POLL_JS, timeout=8)
            finally:
                pc.close()
        except Exception as e:
            log(f"late-capture id={did}: poll error {type(e).__name__}: {e}")
            continue
        if not isinstance(state, dict):
            continue
        txt = state.get("text", "") or ""
        ln = state.get("len", 0)
        if not (ln > 0 and state.get("finalized") and txt != baseline):
            seen_stable_at = None; last_len = ln
            continue
        # Require the length to hold still, so a mid-stream snapshot is not
        # mistaken for the finished reply.
        if ln != last_len:
            last_len = ln; seen_stable_at = time.monotonic(); continue
        if seen_stable_at and (time.monotonic() - seen_stable_at) >= 5.0:
            log(f"late-capture id={did}: RECOVERED reply, {ln} chars — delivering")
            _post_reply(item, txt, {"late_capture": True})
            _last_baseline.pop(did, None)
            return
    log(f"late-capture id={did}: no reply within {deadline_s:.0f}s, giving up")
    _last_baseline.pop(did, None)


def _process_item(item: dict, slot: threading.BoundedSemaphore) -> None:
    """Run one dispatch end-to-end in a worker thread, then post its /reply.
    Releases the concurrency slot when done (success or failure)."""
    tab_id = item.get("tab_id")
    lk = _tab_lock(tab_id)
    with _tab_locks_guard:
        generation = _tab_generation.get(tab_id, 0) + 1
        _tab_generation[tab_id] = generation
    try:
        with lk:  # serialize same-tab dispatches; no-op across distinct tabs
            timing: dict = {"t_polled": time.time()}
            log(f"START id={item.get('id')} tab={tab_id} from={item.get('from_pane')} "
                f"text={item.get('text','')[:60]!r}")
            ok, text_or_err = dispatch_to_sidepanel(item, timing=timing)
        reply_text = text_or_err if ok else f"[bridge error] {text_or_err}"
        timing["t_reply_ready"] = time.time()
        log(f"DONE id={item.get('id')} tab={tab_id} ok={ok} agent={item.get('to_agent')}; "
            f"posting /reply ({len(reply_text)} chars)")
        _post_reply(item, reply_text, timing)
        if not ok:
            # The agent may still be working. Leave a reader on the panel so a
            # reply that lands after the watch died is still delivered, instead
            # of being lost the way the batch 3 and batch 6 reports were.
            threading.Thread(target=_late_capture, args=(item, generation),
                             daemon=True).start()
        else:
            _last_baseline.pop(item.get("id", ""), None)
    except Exception as e:
        log(f"worker crashed id={item.get('id')}: {e}")
    finally:
        slot.release()


def cmd_run() -> int:
    log(f"polling {BRIDGE_URL} for mode=sidepanel items")
    log(f"CDP host: {DEBUG_HOST}:{DEBUG_PORT}")
    log(f"concurrency: up to {CONCURRENCY} dispatch(es) in parallel "
        f"(per-tab serialized)")

    # A bounded semaphore caps how many items we pull into flight at once. We
    # only /poll (which moves an item to the server's in_flight) when a slot is
    # free, so we never strand items the server is waiting on a reply for.
    slot = threading.BoundedSemaphore(CONCURRENCY)
    pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="disp")

    while True:
        slot.acquire()  # wait for a free concurrency slot before polling
        try:
            status, item = bridge("GET", "/poll", mode="sidepanel")
        except Exception as e:
            slot.release()
            log(f"poll error: {e}; backoff {POLL_BACKOFF_S}s")
            time.sleep(POLL_BACKOFF_S); continue
        if status == 204 or item is None:
            slot.release()
            time.sleep(POLL_INTERVAL_S); continue
        # Hand off to a worker; it releases the slot when the dispatch finishes.
        pool.submit(_process_item, item, slot)


def cmd_label(tab_id: int, label: str) -> int:
    """Set the browser tab's <title> via chrome.scripting from the side panel."""
    try:
        host, sp = find_sidepanel_target(tab_id=tab_id)
    except Exception as e:
        print(str(e), file=sys.stderr); return 2
    if not sp:
        print(f"no side panel found for tabId={tab_id}", file=sys.stderr); return 2
    ws = sp["webSocketDebuggerUrl"]
    if host != "127.0.0.1":
        ws = ws.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
    c = CDPClient(ws); c.connect()
    try:
        c.send_command("Runtime.enable", timeout=5)
        # Use chrome.scripting from extension context to set document.title on
        # the associated browser tab. Returns a result object.
        expr = (
            "(async () => {"
            f"  const tabId = {int(tab_id)};"
            f"  const newTitle = {json.dumps(label)};"
            "  try {"
            "    const r = await chrome.scripting.executeScript({"
            "      target: {tabId},"
            "      func: (t) => { document.title = t; return document.title; },"
            "      args: [newTitle],"
            "    });"
            "    return { ok: true, results: r };"
            "  } catch (e) { return { ok: false, error: String(e?.message || e) }; }"
            "})()"
        )
        r = c.send_command("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=10)
        val = r.get("result", {}).get("result", {}).get("value")
        if isinstance(val, dict) and val.get("ok"):
            print(json.dumps({"labeled": True, "tab_id": tab_id, "label": label}))
            return 0
        print(json.dumps({"labeled": False, "detail": val}, indent=2))
        return 1
    finally:
        try: c.close()
        except Exception: pass


def cmd_panels() -> int:
    """List Claude side panels currently open in Chrome (with tabId)."""
    try:
        host, panels = list_sidepanels()
    except Exception as e:
        print(str(e), file=sys.stderr); return 2
    if not panels:
        print("no Claude side panels open"); return 0
    print(f"{'tab_id':>12}  title")
    for p in panels:
        print(f"{str(p['tab_id']):>12}  {p['title'][:60]}")
        print(f"             url={p['url'][:100]}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr); return 2
    cmd = argv[1]
    if cmd == "discover": return cmd_discover()
    if cmd == "targets":  return cmd_targets()
    if cmd == "panels":   return cmd_panels()
    if cmd == "label":
        if len(argv) < 4:
            print("usage: cdp_daemon.py label <tab_id> <label>", file=sys.stderr); return 2
        try: tid = int(argv[2])
        except ValueError:
            print("tab_id must be int", file=sys.stderr); return 2
        return cmd_label(tid, argv[3])
    if cmd == "run":      return cmd_run()
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
