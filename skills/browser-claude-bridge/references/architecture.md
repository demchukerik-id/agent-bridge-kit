# Architecture

## High-level data flow

```
                                                    TAB MODE (default)
┌─ tmux pane ────────────────┐                    ┌─────────────────────┐
│  browser-bridge message <panel> "..."     │ POST /send         │  Chrome extension   │
│  (sets from_pane=$TMUX_PANE)│ ──────────────────►│   service worker    │
└────────────────────────────┘                    │  polls /poll?mode=  │
                                                  │  =tab               │
                                                  └────────┬────────────┘
                                                           │ executeScript
                                                           ▼
                                                  ┌─────────────────────┐
                                                  │  claude.ai tab DOM  │
                                                  │  (content.js)       │
                                                  └────────┬────────────┘
                                                           │ MutationObserver
                                                           ▼  + send button watch
                                                       reply text
                                                           │ POST /reply
                                                           ▼
                                            tmux-bridge → originating pane


                                                  SIDE-PANEL MODE
┌─ tmux pane ────────────────┐
│  browser-bridge message <panel> --mode    │ POST /send {mode:sidepanel}
│  sidepanel "..."           │ ───────────────────────────────────────────┐
└────────────────────────────┘                                            │
                                                                          ▼
                                                         ┌─ bridge server :7777 ─┐
                                                         │  queue split by mode  │
                                                         └────────────┬──────────┘
                                                                      │
                                                /poll?mode=sidepanel  │
                                                                      ▼
                                              ┌─ cdp_daemon.py (WSL) ──────────┐
                                              │  PRE_SEND_JS  (sync evaluate)  │
                                              │  Input.insertText (CDP)         │
                                              │  CLICK_SEND_JS (sync evaluate)  │
                                              │  poll loop (Python) calls       │
                                              │   POLL_JS every 400ms until     │
                                              │   text stable for 3s            │
                                              └────────────────┬───────────────┘
                                                               │ ws://172.22.112.1:9222/...
                                                               ▼
                                              ┌─ relay-9222.py (Windows) ──────┐
                                              │  TCP relay 172.22.112.1:9222   │
                                              │   → 127.0.0.1:9222             │
                                              └────────────────┬───────────────┘
                                                               │
                                                               ▼
                                              ┌─ Chrome (separate profile) ────┐
                                              │  --remote-debugging-port=9222  │
                                              │  CDP target:                   │
                                              │   chrome-extension://fcoe.../  │
                                              │   sidepanel.html               │
                                              └────────────────────────────────┘
```

## Bridge protocol

All endpoints require `Authorization: Bearer <token>` except `/health`.
Token at `~/.config/claude-web-bridge/token`.

| Method | Path | Body / Query | Purpose |
|---|---|---|---|
| POST | `/send` | `{from_pane, text, mode?, tab_id?, tab_match?}` | Enqueue prompt. `mode` defaults to `tab`. |
| GET | `/poll?mode=tab\|sidepanel` | — | Returns next item for that mode, or 204. |
| POST | `/reply` | `{id, from_pane?, text}` | Routes reply back via `tmux-bridge message`. |
| POST | `/tabs` | `{tabs:[{id,title,url,active}]}` | Extension pushes its tab list (every 3s). |
| GET | `/tabs` | — | Returns last-known tab list. |
| GET | `/pin` | — | Current pin (`{url_match, title_hint, set_at}`). |
| POST | `/pin` | `{url_match, title_hint?}` or `{clear:true}` | Set or clear pin. |
| GET | `/health` | — | `{ok, queued, pending_replies, tabs}`. |

State (in-memory + `~/.config/claude-web-bridge/pin.json`):
- `outbox: deque` — items pending dispatch.
- `in_flight: dict` — id → from_pane for items popped but not yet replied.
- `tabs_state` — last list pushed by extension.
- `pin_state` — persisted pin.

## Send-item shape

```json
{
  "id": "uuid-hex",
  "from_pane": "%2",
  "text": "...",
  "mode": "tab" | "sidepanel",
  "tab_id": 123,           // optional, takes precedence
  "tab_match": "/chat/uuid" // optional, used after tab_id; pin sets this
}
```

When mode=sidepanel, tab targeting is irrelevant — there's only one side panel.

## Tab-mode reply detection

`ui/extension/content.js` snapshots the count and last-text-length of
`SELECTORS.assistantTurn` before send, then waits for either a new turn or
the existing one to grow significantly. Streaming-done is detected by a
combination of:

- `button[data-testid="action-bar-copy"]` appearing on the new turn
- Send button re-enabling
- Stop button disappearing

Settle delay 500ms before capture.

## Side-panel reply detection

After send, Python polls `POLL_JS` (returns `{count, len, text}` for the
latest `.claude-response`). Capture happens when:

- `text.length > 0`
- `text !== baseline_text` (set in `PRE_SEND_JS`)
- `text.length` unchanged across consecutive polls for 3 seconds

This survives:

- DOM noise (cursor blinks, animations)
- React re-renders that swap DOM identity
- Side panel keeping only ONE `.claude-response` element (replaced per turn,
  not appended)
- Both short replies and long agent actions (10-min cap)

## Authentication

- Single bearer token, generated once at server startup, persisted to
  `~/.config/claude-web-bridge/token`.
- Server is bound to `127.0.0.1` only — WSL2 forwards localhost so Chrome
  on Windows can reach it.
- Token is the only barrier — any process on the Windows host can hit the
  port. Don't put secrets through this for shared machines.

## Why one extension cannot reach the side panel

Chrome enforces strict isolation between extensions:

- `chrome.scripting.executeScript` refuses targets in another extension's origin.
- `chrome.debugger.attach` errors with *Cannot access a chrome-extension://
  URL of different extension* when targeting a different extension's pages.
- `chrome.runtime.sendMessage(extId, msg)` works only if the recipient
  declares us in `externally_connectable`. Anthropic's extension does not.

Chrome's external `--remote-debugging-port` runs at the **browser** level,
outside the extension sandbox, and so can reach any page including other
extensions' side panels. That's why side-panel mode requires a daemon
*outside* Chrome talking to that port.
