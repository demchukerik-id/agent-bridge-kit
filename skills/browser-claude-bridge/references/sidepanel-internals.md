# Side-panel mode internals

How the CDP daemon drives Anthropic's "Claude in Chrome (Beta)" extension.

## Why this exists

Chrome enforces hard cross-extension isolation. Our extension cannot:
- Inject content scripts into another extension's pages.
- Use `chrome.scripting.executeScript` against another extension's tab.
- Call `chrome.debugger.attach` on a `chrome-extension://<other-id>/...` URL
  (Chrome literally errors with *"Cannot access a chrome-extension:// URL of
  different extension"*).
- Use `chrome.runtime.sendMessage(extId, ...)` unless the recipient declares
  us in `externally_connectable`. Anthropic's extension does not.

Chrome's external `--remote-debugging-port` operates at the browser level
(below the extension sandbox) and so can address any frame in any extension.
That's the only path that works.

## Flow

```
cdp_daemon.py ──► relay-9222.py ──► Chrome debug port ──► sidepanel.html
              ◄──                ◄──                   ◄── reply text
```

1. Daemon polls `/poll?mode=sidepanel`.
2. On item: HTTP `GET 172.22.112.1:9222/json/list`, find the target whose
   `extensionId == fcoeoabgfenejglbffodgkkbkcdhcgfn` and url contains
   `sidepanel.html`.
3. Open WebSocket to the target's `webSocketDebuggerUrl` (host rewritten to
   the relay IP).
4. `Runtime.evaluate(PRE_SEND_JS)` — clears editor, snapshots current
   `.claude-response` text as baseline.
5. `Input.insertText(text)` — types via simulated keyboard input directly
   into the focused element (the editor, focused by step 4). This bypasses
   TipTap/ProseMirror paste-handling fragility.
6. `Runtime.evaluate(CLICK_SEND_JS)` — clicks `button[aria-label="Send message"]`.
7. Python loop: every 400ms `Runtime.evaluate(POLL_JS)` returns
   `{count, len, text}` for the latest `.claude-response`. Capture when
   `text != baseline_text` and `text.length` unchanged for 3s.
8. POST `/reply` to the bridge server, which calls `tmux-bridge message`
   to deliver into the originating pane.

## Selectors

The DOM these depend on is **not** a public API. Patch when the side panel
gets updated.

| Purpose | Selector | Notes |
|---|---|---|
| Editor | `[data-test-id="message-input"]` | TipTap/ProseMirror `contenteditable` div. |
| Send button | `button[aria-label="Send message"]` | Disabled when editor empty. |
| Stop button (during streaming) | `button[aria-label="Stop response"]` (assumed) | Not currently used. |
| Latest assistant turn | `.claude-response` | **Side panel keeps only one** — replaced per turn, not appended. |
| Message bodies (general) | `.prose, [class*="prose"]` | Used for text extraction walk-up. |
| Action-bar copy button | `button[aria-label="Copy message"]` | Appears on each finalized turn (user OR assistant). |
| Feedback (assistant only) | `button[aria-label="Good response"]`, `…="Bad response"` | Only on the **most recent** assistant turn — not a count signal. |

Important quirks we discovered:

- `.claude-response` count stays at **1** in this side panel; new replies
  replace the existing element's content. So *count > baseline* never fires —
  use *text content changed* instead.
- "Good response" / "Bad response" buttons appear on the latest assistant
  turn only. Counting them is unreliable (we tried).
- "Copy message" button counts grow per finalized turn (user + assistant
  separately), but the ratio isn't 1:1 in side panel because user messages
  often get one copy button each but Claude's reply takes a single
  `.claude-response` regardless of how many user messages are pending.

## Why sync polling instead of one big `awaitPromise`

We started with a single `Runtime.evaluate(awaitPromise: true)` containing
the entire wait loop. It hung repeatedly: the WebSocket would block on the
in-flight evaluate for the full duration, and any disturbance (another CDP
client connecting, a transient packet drop) left the daemon stuck.

The current design splits the work into many short synchronous evaluates
(`PRE_SEND_JS`, `CLICK_SEND_JS`, `POLL_JS`) driven by a Python loop. Each
WebSocket round-trip completes in well under a second, the connection is
naturally fault-tolerant, and the daemon emits progress logs every 5
seconds for observability.

## Stability detection rationale

We tried, in order:

1. **`.claude-response` count > baseline** — fails because the side panel
   reuses one element.
2. **MutationObserver on document.body** — fails because of constant page
   noise (caret blinks, ad iframes when on dotabuff, animations).
3. **Text length stable for 1s** — too aggressive, captures mid-stream.
4. **Text length stable for 3s** (current) — works. Polled from Python so
   the CDP connection can't get stuck.

Hard cap is 10 minutes; that's enough for multi-step agent actions like
"open Gmail, read the last 2 emails, summarize" without the daemon falsely
giving up.

## CDP client (`daemon/cdp.py`)

Stdlib-only WebSocket client implementing just enough of RFC 6455 to drive
CDP:

- Text frames only (CDP is JSON over text).
- Client-to-server frames are masked (per RFC).
- Server-to-client frames are unmasked.
- Handles ping/pong, close, fragmented frames.
- One reader thread per connection demuxes responses (by `id`) from events
  (no `id`).
- `send_command(method, params, timeout)` is a synchronous RPC.
- Connection per dispatch — opened in `dispatch_to_sidepanel`, closed in
  `finally`. Avoids stale-connection issues across dispatches.

No third-party deps. The whole client is ~250 lines.

## Known fragility

- Anthropic ships side-panel updates often. Any class rename or attribute
  change will break selectors. Watch for selector-related bridge errors
  and patch `cdp_daemon.py:SELECTORS`-equivalent constants in the
  PRE_SEND_JS / CLICK_SEND_JS / POLL_JS string blocks.
- Chrome version updates may close the debug-port hole. Chrome 136 added
  the user-data-dir restriction; future versions may add more.
- The yellow "is being debugged" infobar can be dismissed by the user but
  reappears. There is no Chrome flag to suppress it permanently.
- If the user closes the side panel, the CDP target disappears and the
  next dispatch errors with "side panel target not found." The daemon
  recovers and routes the error back via tmux-bridge.
