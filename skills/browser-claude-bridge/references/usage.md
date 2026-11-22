# Usage

All commands run from inside a tmux pane, so `$TMUX_PANE` is set and the
bridge knows where to paste the reply.

## Quick reference

```
browser-bridge list                         panels the bridge sees, with their /brN aliases
browser-bridge message <panel> "text"       send; the reply is pasted into your pane when the panel finishes
browser-bridge read <panel>                 print the last reply captured from a panel
browser-bridge type <panel> "text"          put text in the panel's composer without sending
browser-bridge keys <panel> Enter           press a key in the panel (Enter, Escape, Tab)
browser-bridge name <panel> <label>         rename a panel in panels.json and in the Chrome tab group
browser-bridge resolve <label>              print the tab_id behind a name
browser-bridge id                           this pane's sender identity
browser-bridge doctor                       health check; restarts a dead forwarder by itself
browser-bridge discover                     print what the panel's DOM currently exposes (selector drift)
browser-bridge forget <panel>               drop one panel from panels.json
browser-bridge forget-stale --yes           drop every panel whose tab is gone
browser-bridge kill-agents --yes            close every registered panel tab, Chrome stays
browser-bridge kill-all --yes               close the panel tabs and every bridge Chrome window
bridge-daemon status|stop|start|restart     the only safe way to restart the CDP daemon
```

`<panel>` is a registered name (`pane-0`), a short alias (`/br14`), or a raw
tab id. Names must match `[A-Za-z0-9_-]+`, so a panel bound to pane `%0` is
called `pane-0`, not `%0`.

## Sending and getting the answer

```bash
browser-bridge list
#   SHORT  NAME     TAB_ID
#   /br14  pane-0   1635164807

browser-bridge message pane-0 "Reply with one line: OK <your active tab title>"
# sent to pane-0 (id ...). The reply is pasted into pane %0 when the panel finishes
```

Write the message the way the user would type it into the chat: plain first-person
sentences, no bracketed tags or `from:` headers, no "You are ...", no reply
format to follow, no skill to load. The panel gets the text verbatim, and
Claude for Chrome refuses machine-shaped text in its composer as a prompt
injection planted by the page. The server tags the reply itself.

The reply arrives in your pane as one line headed `[browser-bridge /br14 → %0]`,
submitted, even while you are mid-turn (Claude Code queues it). Errors arrive
the same way, headed `error:`. Nothing is buffered for `GET /reply` when the
sender is a tmux pane, so do not poll for it; `browser-bridge read pane-0`
shows the last captured reply if you need it again.

If a dispatch is refused you get the reason immediately:

- `editor not found ... (panel has no conversation yet)`: type one message in
  the panel by hand, then resend.
- `editor not found at chrome-extension://.../sidepanel.html`: the daemon is on
  old code; `bridge-daemon restart`.
- `forbidden: panel ... owned by '...'`: another pane owns the panel; add
  `--force` or set the relay target in the extension popup.

## Long replies and images

A panel that browses, generates an image, or thinks for minutes can take
longer than any CLI wait. The daemon keeps watching for up to 30 minutes and
pastes the reply when it lands. Progress: `tail -f /tmp/cdp.log`, look for
`DONE id=... ok=True`.

## Naming a panel after the pane it serves

```bash
browser-bridge name /br14 pane-0
```

This renames the entry in `panels.json` and the Chrome tab group title, so
`browser-bridge list` and the tab strip agree.
