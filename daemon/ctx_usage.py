#!/usr/bin/env python3
"""
ctx-usage — report each browser agent's context-window usage.

The number is not an estimate. Every turn the Claude side panel streams back
an SSE frame whose `usage.input_tokens` is exactly what the model read for
that turn: system prompt, tool results, page reads, the whole conversation.
This process installs a small capture hook inside each side panel (see
ctx_probe.js), reads what the hook has collected, and POSTs it to the bridge
server, which serves it at /panels/ctx for the popup and the dashboard.

Why a separate process and not the extension: Chrome refuses to let one
extension attach the debugger to another extension's page ("Cannot access a
chrome-extension:// URL of different extension"), so the bridge extension can
never read the Claude side panel. CDP from outside Chrome can.

Reads CDP_HOST / CDP_PORT the same way cdp_daemon.py does, so it goes through
the same Windows forwarder (9334 -> Chrome's 9333).

  ctx_usage.py            poll forever
  ctx_usage.py --once     one pass, print the result
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cdp import CDPClient, list_targets  # type: ignore

CLAUDE_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
BRIDGE_URL = os.environ.get("CLAUDE_BRIDGE_URL", "http://127.0.0.1:7777")
TOKEN_FILE = Path.home() / ".config" / "claude-web-bridge" / "token"
CTX_WINDOW_TOKENS = int(os.environ.get("CTX_WINDOW_TOKENS", "200000"))
INTERVAL_S = float(os.environ.get("CTX_INTERVAL_S", "10"))
PROBE = (HERE / "ctx_probe.js").read_text(encoding="utf-8")


def _detect_windows_host() -> str | None:
    try:
        out = os.popen("ip route show default").read().split()
        return out[2] if len(out) > 2 else None
    except Exception:
        return None


def _hosts() -> list[str]:
    explicit = os.environ.get("CDP_HOST")
    if explicit:
        return [explicit]
    hosts = ["127.0.0.1"]
    win = _detect_windows_host()
    if win and win not in hosts:
        hosts.append(win)
    return hosts


PORT = int(os.environ.get("CDP_PORT", "9334"))


def log(*a):
    print("[ctx-usage]", *a, file=sys.stderr, flush=True)


def post_bridge(usage: dict, diag: dict) -> None:
    body = json.dumps({"usage": usage, "diag": diag}).encode()
    req = urllib.request.Request(
        BRIDGE_URL + "/panels/ctx", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + TOKEN_FILE.read_text().strip()})
    urllib.request.urlopen(req, timeout=5).read()


def panel_tab_id(url: str) -> int | None:
    m = re.search(r"[?&]tabId=(\d+)", url or "")
    return int(m.group(1)) if m else None


def read_panel(host: str, target: dict) -> dict | None:
    """Install the hook if absent, return whatever it has captured."""
    ws = target["webSocketDebuggerUrl"]
    if host != "127.0.0.1":
        ws = ws.replace("ws://127.0.0.1:", f"ws://{host}:", 1)
    c = CDPClient(ws)
    c.connect()
    try:
        c.send_command("Runtime.enable", timeout=5)
        r = c.send_command("Runtime.evaluate", {
            "expression": PROBE, "returnByValue": True, "awaitPromise": False,
        }, timeout=8)
        return (r.get("result") or {}).get("result", {}).get("value")
    finally:
        c.close()


def one_pass() -> dict:
    usage: dict = {}
    diag: dict = {"source": "ctx_usage.py", "at": int(time.time() * 1000), "errors": []}
    targets, host = None, None
    for h in _hosts():
        try:
            targets = list_targets(h, PORT)
            host = h
            break
        except Exception as e:
            diag["errors"].append(f"{h}:{PORT} {e}")
    if targets is None:
        diag["panels_seen"] = 0
        return {"usage": usage, "diag": diag}

    diag["errors"] = []          # a working host clears the probing noise
    diag["cdp"] = f"{host}:{PORT}"
    panels = [t for t in targets
              if (t.get("url") or "").startswith(f"chrome-extension://{CLAUDE_EXTENSION_ID}/")
              and "sidepanel" in (t.get("url") or "").lower()]
    diag["panels_seen"] = len(panels)
    diag["panel_tab_ids"] = [panel_tab_id(t.get("url", "")) for t in panels]

    for t in panels:
        tid = panel_tab_id(t.get("url", ""))
        if tid is None:
            continue
        try:
            st = read_panel(host, t)
            if st and isinstance(st.get("input_tokens"), int):
                usage[str(tid)] = {
                    "pct": min(999, round(st["input_tokens"] / CTX_WINDOW_TOKENS * 100)),
                    "input_tokens": st["input_tokens"],
                    "output_tokens": st.get("output_tokens"),
                    "at": st.get("at"),
                }
            else:
                # Hook is in place; this chat has not run a turn yet.
                usage[str(tid)] = {"pct": None, "input_tokens": None, "at": 0}
        except Exception as e:
            diag["errors"].append(f"tab {tid}: {e}")
    return {"usage": usage, "diag": diag}


def main() -> int:
    once = "--once" in sys.argv
    while True:
        r = one_pass()
        try:
            post_bridge(r["usage"], r["diag"])
        except Exception as e:
            r["diag"]["errors"].append(f"post failed: {e}")
        if once:
            print(json.dumps(r, indent=2))
            return 0
        if r["diag"]["errors"]:
            log(json.dumps(r["diag"])[:300])
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
