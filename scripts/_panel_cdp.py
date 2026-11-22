"""Shared CDP helpers for bridge-open-panel and bridge-close-panel.

Imported, never run. Talks straight to the bridge Chrome's debug port (9334
from WSL) and uses daemon/cdp.py for the websocket.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "daemon"))
from cdp import CDPClient  # noqa: E402

EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"  # Claude for Chrome
PORT = 9334
PS_SCRIPT = REPO / "windows/window-keys.ps1"
ACCOUNTS_FILE = Path.home() / ".config/claude-web-bridge/accounts.json"


def _accounts() -> dict:
    """accounts.json: {"default": "<email>", "window_names": {"<email>": "<title>"}}."""
    try:
        return json.loads(ACCOUNTS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def default_account() -> str:
    """The account --account defaults to: BRIDGE_ACCOUNT, else "default" in accounts.json."""
    return os.environ.get("BRIDGE_ACCOUNT") or _accounts().get("default", "")


def resolve_account(name: str) -> str:
    """An email, or an alias from accounts.json "aliases" (e.g. "login")."""
    return _accounts().get("aliases", {}).get(name, name)


def die(prog: str, msg: str) -> None:
    print(f"{prog}: {msg}", file=sys.stderr)
    sys.exit(1)


def cdp_host() -> str:
    """The Windows host first (WSL), then 127.0.0.1. Empty if neither answers."""
    out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout.split()
    for host in ([out[2]] if len(out) > 2 else []) + ["127.0.0.1"]:
        try:
            urllib.request.urlopen(f"http://{host}:{PORT}/json/version", timeout=2)
            return host
        except OSError:
            pass
    return ""


def targets(host: str) -> list[dict]:
    with urllib.request.urlopen(f"http://{host}:{PORT}/json/list", timeout=3) as r:
        return json.load(r)


def panel_ids(host: str) -> set[str]:
    """Tab ids that currently have a Claude side panel open."""
    return {t["url"].split("tabId=")[-1] for t in targets(host)
            if t["url"].startswith(f"chrome-extension://{EXT_ID}/sidepanel.html")}


def connect(host: str, target: dict) -> CDPClient:
    ws = target["webSocketDebuggerUrl"]
    ws = ws.replace("127.0.0.1:9333", f"{host}:{PORT}").replace("localhost:9333", f"{host}:{PORT}")
    c = CDPClient(ws)
    c.connect()
    return c


def evaluate(c: CDPClient, expr: str):
    """Run JS and return its value. Raises RuntimeError on a JS exception."""
    r = c.send_command("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
    res = r.get("result", {})
    if "exceptionDetails" in res:
        raise RuntimeError(res["exceptionDetails"].get("exception", {}).get("description")
                           or res["exceptionDetails"].get("text"))
    return res.get("result", {}).get("value")


def close_target(host: str, target_id: str) -> None:
    urllib.request.urlopen(f"http://{host}:{PORT}/json/close/{target_id}", timeout=3)


def wait_for(fn, timeout: float, step: float = 0.1):
    end = time.time() + timeout
    while True:
        v = fn()
        if v or time.time() >= end:
            return v
        time.sleep(step)


def window_name(email: str) -> str:
    """The bridge window title for an account. Same rule as wname() in
    bridge-profiles, which sets it with --window-name. accounts.json
    "window_names" overrides it per account."""
    named = _accounts().get("window_names", {}).get(email)
    if named:
        return named
    return f"{email.split('@')[0]}@ - bridge pfl"


def window_keys(win: str, keys: list[str], restore: bool = False) -> str:
    """Bring window `win` to the front and press `keys` in it; [] only focuses.
    restore: hand focus back to the window that had it once the keys are in.
    Returns "" on success, else the reason nothing was pressed."""
    head = (f"$WindowName = '{win.replace(chr(39), chr(39) * 2)}'\n$Keys = @({', '.join(repr(k) for k in keys)})\n"
            f"$Restore = ${'true' if restore else 'false'}\n")
    enc = base64.b64encode((head + PS_SCRIPT.read_text()).encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", enc],
                       capture_output=True, text=True, cwd="/mnt/c")
    if r.returncode != 0:
        return r.stdout.strip() or r.stderr.strip() or "window-keys.ps1 failed"
    return ""
